import argparse
import base64
import os
import subprocess
import sys
import tempfile

import requests
import yaml
BASE_URL = os.environ.get("CONFLUENCE_URL")
TOKEN = os.environ.get("CONFLUENCE_TOKEN")

if not BASE_URL:
    BASE_URL = "https://wiki.infiscale.dev/"

session = requests.Session()

session.headers.update(
    {
        "Authorization": f"Bearer {TOKEN}",
    }
)

if not all([BASE_URL, TOKEN]):
    print("Error: 环境变量 CONFLUENCE_TOKEN 必须设置")
    sys.exit(1)


def get_id(init_id):
    tiny_id = init_id.split("/")[-1]
    try:
        padding = len(tiny_id) % 4
        if padding > 0:
            tiny_id += "=" * (4 - padding)
        tiny_id = tiny_id.replace("-", "+").replace("_", "/")
        decoded_bytes = base64.b64decode(tiny_id)

        page_id = int.from_bytes(decoded_bytes, byteorder="little")
        print(f"Real ID of {init_id}: {page_id}")

        return str(page_id)
    except Exception as exc:
        print(f"Error: {exc}")
        return None


def get_page_title_by_id(page_id):
    """
    关键函数：通过 ID 查询页面的当前标题
    """
    if not page_id:
        return None

    page_id = get_id(page_id)
    if not page_id:
        return None

    api_url = f"{BASE_URL}rest/api/content/{page_id}"
    try:
        resp = session.get(api_url)
        if resp.status_code == 200:
            return resp.json().get("title")
        if resp.status_code == 404:
            print(f"Error: Parent ID {page_id} 不存在！")
            return None
        print(f"Error: 查询父页面失败 {resp.status_code}: {resp.text}")
        return None
    except Exception as exc:
        print(f"Error connecting to API: {exc}")
        return None


def is_fence_line(line):
    trimmed = line.lstrip()
    while trimmed.startswith(">"):
        trimmed = trimmed[1:].lstrip()

    if len(trimmed) < 3:
        return None

    fence_char = trimmed[0]
    if fence_char not in ("`", "~"):
        return None

    count = 0
    while count < len(trimmed) and trimmed[count] == fence_char:
        count += 1

    if count < 3:
        return None

    return fence_char, count


def is_fence_close(line, fence_char, fence_len):
    trimmed = line.lstrip()
    while trimmed.startswith(">"):
        trimmed = trimmed[1:].lstrip()

    if len(trimmed) < fence_len:
        return False

    count = 0
    while count < len(trimmed) and trimmed[count] == fence_char:
        count += 1

    if count < fence_len:
        return False

    return trimmed[count:].strip() == ""


PLACEHOLDER_INCLUDE_RE = re.compile(r"<!--\s*Include:\s*<[^>]+>\s*-->")


def mask_placeholder_includes(content):
    return PLACEHOLDER_INCLUDE_RE.sub(
        lambda match: match.group(0).replace("<!-- Include:", "< !-- Include:"),
        content,
    )


def mask_all_html_comments(content):
    return content.replace("<!--", "< !--")


def mask_includes_in_code_blocks(content):
    content = mask_all_html_comments(content)
    content = mask_placeholder_includes(content)
    lines = content.splitlines(keepends=True)
    out = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in lines:
        fence = is_fence_line(line)
        if not in_fence and fence:
            in_fence = True
            fence_char, fence_len = fence
        elif in_fence and is_fence_close(line, fence_char, fence_len):
            in_fence = False

        is_indented_code = line.startswith("\t") or line.startswith("    ")
        is_placeholder_include = PLACEHOLDER_INCLUDE_RE.search(line) is not None
        if (in_fence or is_indented_code or is_placeholder_include) and "<!-- Include:" in line:
            line = line.replace("<!-- Include:", "< !-- Include:")

        out.append(line)

    return "".join(out)


def generate_mark_config(yaml_config_path, tag):
    """
    read yaml, generate data structure for mark tools
    """
    with open(yaml_config_path, "r", encoding="utf-8") as handle:
        config_data = yaml.safe_load(handle)

    default_space = config_data.get("default_space")

    pages_to_sync = []

    print(f"--- Preparing Sync for Tag: {tag} ---")

    for rule in config_data["rules"]:
        space = rule.get("space", default_space)
        source_path = rule["path"]
        parent_id = rule.get("parent_id")

        source_paths = [source_path]

        if not source_paths:
            print(f"Checking: {source_path} ... NOT FOUND (Skipping)")
            continue

        parent_title = get_page_title_by_id(parent_id)
        if not parent_title:
            print(f"Checking: Page {parent_id} ... NOT FOUND (Skipping)")
            continue

        for actual_path in source_paths:
            if not os.path.exists(actual_path):
                print(f"Checking: {actual_path} ... NOT FOUND (Skipping)")
                continue

            page_entry = {
                "path": actual_path,
                "space": space,
                "parent": parent_title,
            }
            pages_to_sync.append(page_entry)

    if not pages_to_sync:
        print("No new pages to publish.")

    return pages_to_sync


def append_version(page, tag):
    path = page["path"]
    dir_path = os.path.dirname(path)
    filename = os.path.basename(path)

    version = f"<!-- Title: {filename}_{tag} -->\n"
    with open(path, "r", encoding="utf-8") as handle:
        original_content = handle.read()

    original_content = "\n" + mask_includes_in_code_blocks(original_content)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        dir=dir_path,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        temp_file.write(version + original_content)
        temp_file_path = temp_file.name

    page["path"] = temp_file_path
    page["title"] = f"{filename}_{tag}"
    return page, filename


def run_mark_tool(page):
    """调用 mark 二进制文件执行同步"""
    print("\n--- Running mark ---")
    try:
        subprocess.run(
            [
                "mark",
                "-b",
                BASE_URL,
                "-p",
                TOKEN,
                "-f",
                page["path"],
                "--space",
                page["space"],
                "--parents",
                page["parent"],
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Error running mark tool: {exc}")
        sys.exit(1)
    finally:
        if os.path.exists(page["path"]):
            os.remove(page["path"])


def update_landing_page(page, newpage):
    title = page["parent"]
    storage_value = (
        f"<!-- Title: {title} -->\n\n"
        "<strong>Current Latest Version:</strong> "
        f'<ac:link><ri:page ri:content-title="{newpage["title"]}" '
        f'ri:space-key="{newpage["space"]}"/></ac:link>\n'
        '<ac:structured-macro ac:name="children" ac:schema-version="1">\n'
        '<ac:parameter ac:name="sort">creation</ac:parameter>\n'
        '<ac:parameter ac:name="reverse">true</ac:parameter>\n'
        "</ac:structured-macro>\n"
    )
    path = page["path"]
    dir_path = os.path.dirname(path)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        dir=dir_path,
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        temp_file.write(storage_value)
        temp_file_path = temp_file.name

    page["path"] = temp_file_path
    page["parent"] = ""
    return page


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()

    pages_to_sync = generate_mark_config(args.config, args.tag)
    if pages_to_sync:
        for page in pages_to_sync:
            newpage, _title = append_version(page, args.tag)
            run_mark_tool(newpage)
            landing_page = update_landing_page(page, newpage)
            run_mark_tool(landing_page)
    else:
        print("Skipped execution.")


if __name__ == "__main__":
    main()
