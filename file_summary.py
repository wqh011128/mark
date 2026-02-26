import argparse
import html
import re
from urllib.parse import urljoin

from mark_sync import BASE_URL, get_id, get_page_title_by_id, session

TAG_RE = re.compile(r"<[^>]+>")
BACKLINK_START = "<!-- summary-backlink:start -->"
BACKLINK_END = "<!-- summary-backlink:end -->"
BACKLINK_BLOCK_RE = re.compile(
    rf"{re.escape(BACKLINK_START)}.*?{re.escape(BACKLINK_END)}", re.DOTALL
)
BACKLINK_TEXT_RE = re.compile(r"\[\s*link\s+to\s+summary\s+table\s*\]", re.IGNORECASE)


def list_child_pages(parent_id):
    resolved_id = get_id(parent_id)
    if not resolved_id:
        return []

    children = []
    start = 0
    limit = 50
    while True:
        api_url = f"{BASE_URL}rest/api/content/{resolved_id}/child/page"
        params = {"start": start, "limit": limit}
        resp = session.get(api_url, params=params)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        children.extend(results)
        if data.get("_links", {}).get("next"):
            start += limit
            continue
        break
    return children


def strip_html(text):
    return TAG_RE.sub("", text).strip()


def normalize_comment_text(raw_storage):
    without_managed_block = BACKLINK_BLOCK_RE.sub("", raw_storage)
    plain_text = strip_html(without_managed_block)
    plain_text = BACKLINK_TEXT_RE.sub("", plain_text)
    return " ".join(plain_text.split())


def has_child_pages(page_id):
    api_url = f"{BASE_URL}rest/api/content/{page_id}/child/page"
    resp = session.get(api_url, params={"start": 0, "limit": 1})
    resp.raise_for_status()
    data = resp.json()
    return bool(data.get("results"))


def list_comments(page_id):
    comments_by_id = {}

    def fetch_comments(api_url):
        start = 0
        limit = 50
        while True:
            params = {"start": start, "limit": limit, "expand": "body.storage,history,version"}
            resp = session.get(api_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            for item in results:
                comment_id = item.get("id")
                if comment_id:
                    comments_by_id[comment_id] = item
            if data.get("_links", {}).get("next"):
                start += limit
                continue
            break

    # top-level comments on the page
    fetch_comments(f"{BASE_URL}rest/api/content/{page_id}/child/comment")
    # nested replies to comments (inline replies/threaded comments)
    fetch_comments(f"{BASE_URL}rest/api/content/{page_id}/descendant/comment")

    return list(comments_by_id.values())


def get_comment_author_id(comment):
    created_by = comment.get("history", {}).get("createdBy", {})
    return (
        created_by.get("accountId")
        or created_by.get("userKey")
        or created_by.get("username")
        or created_by.get("email")
        or "-"
    )


def get_comment_link(comment, page_id):
    webui_path = comment.get("_links", {}).get("webui", "")
    if webui_path:
        return urljoin(BASE_URL, webui_path.lstrip("/"))

    comment_id = comment.get("id")
    if comment_id:
        return f"{BASE_URL}pages/viewpage.action?pageId={page_id}#comment-{comment_id}"

    return f"{BASE_URL}pages/viewpage.action?pageId={page_id}"


def build_row_anchor(comment_id):
    return f"summary-comment-{comment_id}"


def build_child_page_rows(children):
    if not children:
        return []

    rows = []
    for index, page in enumerate(children, start=1):
        title = page.get("title", "")
        page_id = page.get("id", "")
        if has_child_pages(page_id):
            rows.append((index, title, page_id, "-", "-", "", None, None, None, None))
            continue

        comments = list_comments(page_id)
        if not comments:
            rows.append((index, title, page_id, "-", "-", "", None, None, None, None))
            continue

        for comment in comments:
            value = comment.get("body", {}).get("storage", {}).get("value", "")
            cleaned = normalize_comment_text(value) or "-"
            author_id = get_comment_author_id(comment)
            comment_link = get_comment_link(comment, page_id)
            comment_id = comment.get("id")
            row_anchor = build_row_anchor(comment_id) if comment_id else None
            comment_version = comment.get("version", {}).get("number")
            rows.append(
                (
                    index,
                    title,
                    page_id,
                    cleaned,
                    author_id,
                    comment_link,
                    comment_id,
                    row_anchor,
                    value,
                    comment_version,
                )
            )

    return rows


def get_parent_context(parent_id):
    resolved_id = get_id(parent_id)
    if not resolved_id:
        return None

    api_url = f"{BASE_URL}rest/api/content/{resolved_id}"
    resp = session.get(api_url, params={"expand": "space,ancestors"})
    resp.raise_for_status()
    data = resp.json()
    space_key = data.get("space", {}).get("key")
    ancestors = data.get("ancestors", [])
    if ancestors:
        target_parent_id = ancestors[-1].get("id")
    else:
        target_parent_id = None
    parent_title = data.get("title", "")
    return {
        "space_key": space_key,
        "target_parent_id": target_parent_id,
        "parent_title": parent_title,
    }


def build_storage_table(rows):
    header = (
        "<table>"
        "<thead><tr>"
        "<th>#</th><th>Title</th><th>ID</th><th>Comment</th><th>Commenter</th>"
        "</tr></thead><tbody>"
    )
    body_parts = []
    for row in rows:
        comment_text = html.escape(row[3])
        comment_link = row[5]
        row_anchor = row[7]
        if comment_text != "-" and comment_link:
            comment_cell = (
                f"{comment_text} "
                f'[<a href="{html.escape(comment_link, quote=True)}">link to file</a>]'
            )
        else:
            comment_cell = "-"

        tr_open = f'<tr id="{html.escape(row_anchor, quote=True)}">' if row_anchor else "<tr>"

        body_parts.append(
            tr_open
            + f"<td>{html.escape(str(row[0]))}</td>"
            + f"<td>{html.escape(row[1])}</td>"
            + f"<td>{html.escape(row[2])}</td>"
            + f"<td>{comment_cell}</td>"
            + f"<td>{html.escape(row[4])}</td>"
            + "</tr>"
        )
    footer = "</tbody></table>"
    return header + "".join(body_parts) + footer


def find_existing_summary_page(space_key, title, target_parent_id):
    api_url = f"{BASE_URL}rest/api/content"
    params = {
        "title": title,
        "spaceKey": space_key,
        "type": "page",
        "expand": "version,ancestors",
    }
    resp = session.get(api_url, params=params)
    resp.raise_for_status()
    candidates = resp.json().get("results", [])

    for candidate in candidates:
        ancestors = candidate.get("ancestors", [])
        direct_parent_id = ancestors[-1].get("id") if ancestors else None
        if direct_parent_id == target_parent_id:
            return candidate

    return None


def update_summary_page(existing_page, summary_title, storage_value, target_parent_id):
    version = existing_page.get("version", {}).get("number", 1)
    payload = {
        "id": existing_page.get("id"),
        "type": "page",
        "title": summary_title,
        "version": {"number": version + 1},
        "body": {"storage": {"value": storage_value, "representation": "storage"}},
    }
    if target_parent_id:
        payload["ancestors"] = [{"id": target_parent_id}]

    api_url = f"{BASE_URL}rest/api/content/{existing_page.get('id')}"
    resp = session.put(api_url, json=payload)
    resp.raise_for_status()
    print(f"Updated summary page: {existing_page.get('id')}")
    return existing_page.get("id")


def upsert_summary_page(parent_context, rows):
    if not parent_context:
        print("Parent page not found.")
        return None

    if not rows:
        rows = [("-", "-", "-", "-", "-", "", None, None, None, None)]

    summary_title = f"{parent_context['parent_title']} - Child Page Summary"
    storage_value = build_storage_table(rows)

    existing_page = find_existing_summary_page(
        parent_context["space_key"],
        summary_title,
        parent_context["target_parent_id"],
    )

    if existing_page:
        return update_summary_page(
            existing_page,
            summary_title,
            storage_value,
            parent_context["target_parent_id"],
        )

    payload = {
        "type": "page",
        "title": summary_title,
        "space": {"key": parent_context["space_key"]},
        "body": {"storage": {"value": storage_value, "representation": "storage"}},
    }
    if parent_context["target_parent_id"]:
        payload["ancestors"] = [{"id": parent_context["target_parent_id"]}]

    api_url = f"{BASE_URL}rest/api/content"
    resp = session.post(api_url, json=payload)
    resp.raise_for_status()
    page_id = resp.json().get("id", "")
    print(f"Created summary page: {page_id}")
    return page_id


def build_summary_anchor_url(summary_page_id, row_anchor):
    return f"{BASE_URL}pages/viewpage.action?pageId={summary_page_id}#{row_anchor}"


def upsert_backlink_block(original_storage, summary_url):
    cleaned_storage = BACKLINK_BLOCK_RE.sub("", original_storage)
    cleaned_storage = BACKLINK_TEXT_RE.sub("", cleaned_storage)

    backlink_block = (
        f"{BACKLINK_START}"
        f" <a href=\"{html.escape(summary_url, quote=True)}\">[link to summary table]</a>"
        f"{BACKLINK_END}"
    )

    return cleaned_storage + backlink_block


def update_comment_backlinks(rows, summary_page_id):
    if not summary_page_id:
        return

    for row in rows:
        comment_id = row[6]
        row_anchor = row[7]
        original_storage = row[8]
        version_number = row[9]

        if not comment_id or not row_anchor or original_storage is None or version_number is None:
            continue

        summary_url = build_summary_anchor_url(summary_page_id, row_anchor)
        new_storage = upsert_backlink_block(original_storage, summary_url)
        if new_storage == original_storage:
            continue

        payload = {
            "id": comment_id,
            "type": "comment",
            "version": {"number": int(version_number) + 1},
            "body": {"storage": {"value": new_storage, "representation": "storage"}},
        }
        api_url = f"{BASE_URL}rest/api/content/{comment_id}"
        resp = session.put(api_url, json=payload)
        resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-id", required=True, help="Confluence parent page ID to list child pages")
    args = parser.parse_args()

    parent_title = get_page_title_by_id(args.parent_id)
    if not parent_title:
        print("Parent page not found.")
        return

    children = list_child_pages(args.parent_id)
    rows = build_child_page_rows(children)
    parent_context = get_parent_context(args.parent_id)
    summary_page_id = upsert_summary_page(parent_context, rows)
    update_comment_backlinks(rows, summary_page_id)


if __name__ == "__main__":
    main()
