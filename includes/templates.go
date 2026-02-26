package includes

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"text/template"

	"gopkg.in/yaml.v3"

	"github.com/reconquest/karma-go"
	"github.com/reconquest/pkg/log"
)

// <!-- Include: <template path>
//
//	(Delims: (none | "<left>","<right>"))?
//	<optional yaml data> -->
var reIncludeDirective = regexp.MustCompile(
	`(?s)` +
		`<!--\s*Include:\s*(?P<template>.+?)\s*` +
		`(?:\n\s*Delims:\s*(?:(none|"(?P<left>.*?)"\s*,\s*"(?P<right>.*?)")))?\s*` +
		`(?:\n(?P<config>.*?))?-->`,
)

func LoadTemplate(
	base string,
	includePath string,
	path string,
	left string,
	right string,
	templates *template.Template,
) (*template.Template, error) {
	var (
		name  = strings.TrimSuffix(path, filepath.Ext(path))
		facts = karma.Describe("name", name)
	)

	if template := templates.Lookup(name); template != nil {
		return template, nil
	}

	var body []byte

	body, err := os.ReadFile(filepath.Join(base, path))
	if err != nil {
		if includePath != "" {
			body, err = os.ReadFile(filepath.Join(includePath, path))
		}
		if err != nil {
			err = facts.Format(
				err,
				"unable to read template file",
			)
			return nil, err
		}

	}

	body = bytes.ReplaceAll(
		body,
		[]byte("\r\n"),
		[]byte("\n"),
	)

	templates, err = templates.New(name).Delims(left, right).Parse(string(body))
	if err != nil {
		err = facts.Format(
			err,
			"unable to parse template",
		)

		return nil, err
	}

	return templates, nil
}

func ProcessIncludes(
	base string,
	includePath string,
	contents []byte,
	templates *template.Template,
) (*template.Template, []byte, bool, error) {
	stripBlockQuote := func(line []byte) []byte {
		trimmed := bytes.TrimLeft(line, " \t")
		for len(trimmed) > 0 && trimmed[0] == '>' {
			trimmed = bytes.TrimLeft(trimmed[1:], " \t")
		}

		return trimmed
	}

	isFenceLine := func(line []byte) (byte, int, bool) {
		trimmed := stripBlockQuote(line)
		if len(trimmed) < 3 {
			return 0, 0, false
		}

		switch trimmed[0] {
		case '`', '~':
		default:
			return 0, 0, false
		}

		char := trimmed[0]
		count := 0
		for count < len(trimmed) && trimmed[count] == char {
			count++
		}

		if count < 3 {
			return 0, 0, false
		}

		return char, count, true
	}

	isFenceClose := func(line []byte, fenceChar byte, fenceLen int) bool {
		trimmed := stripBlockQuote(line)
		if len(trimmed) < fenceLen {
			return false
		}

		count := 0
		for count < len(trimmed) && trimmed[count] == fenceChar {
			count++
		}

		if count < fenceLen {
			return false
		}

		rest := bytes.TrimSpace(trimmed[count:])
		return len(rest) == 0
	}

	vardump := func(
		facts *karma.Context,
		data map[string]interface{},
	) *karma.Context {
		for key, value := range data {
			key = "var " + key
			facts = facts.Describe(
				key,
				strings.ReplaceAll(
					fmt.Sprint(value),
					"\n",
					"\n"+strings.Repeat(" ", len(key)+2),
				),
			)
		}

		return facts
	}

	var (
		recurse bool
		err     error
	)

	processSegment := func(segment []byte) []byte {
		return reIncludeDirective.ReplaceAllFunc(
			segment,
			func(spec []byte) []byte {
				if err != nil {
					return nil
				}

				groups := reIncludeDirective.FindSubmatch(spec)

				var (
					path       = string(groups[1])
					delimsNone = string(groups[2])
					left       = string(groups[3])
					right      = string(groups[4])
					config     = groups[5]
					data       = map[string]interface{}{}

					facts = karma.Describe("path", path)
				)

				if delimsNone == "none" {
					left = "\x00"
					right = "\x01"
				}

				err = yaml.Unmarshal(config, &data)
				if err != nil {
					err = facts.
						Describe("config", string(config)).
						Format(
							err,
							"unable to unmarshal template data config",
						)

					return nil
				}

				log.Tracef(vardump(facts, data), "including template %q", path)

				templates, err = LoadTemplate(base, includePath, path, left, right, templates)
				if err != nil {
					err = facts.Format(err, "unable to load template")
					return nil
				}

				var buffer bytes.Buffer

				err = templates.Execute(&buffer, data)
				if err != nil {
					err = vardump(facts, data).Format(
						err,
						"unable to execute template",
					)

					return nil
				}

				recurse = true

				return buffer.Bytes()
			},
		)
	}

	lines := bytes.Split(contents, []byte("\n"))
	var output bytes.Buffer
	var segment bytes.Buffer
	inFence := false
	fenceChar := byte(0)
	fenceLen := 0

	flushSegment := func() {
		if segment.Len() == 0 {
			return
		}

		output.Write(processSegment(segment.Bytes()))
		segment.Reset()
	}

	for index, line := range lines {
		hasNewline := index < len(lines)-1

		if inFence {
			output.Write(line)
			if hasNewline {
				output.WriteByte('\n')
			}

			if isFenceClose(line, fenceChar, fenceLen) {
				inFence = false
			}

			continue
		}

		if char, count, ok := isFenceLine(line); ok {
			flushSegment()
			inFence = true
			fenceChar = char
			fenceLen = count

			output.Write(line)
			if hasNewline {
				output.WriteByte('\n')
			}

			continue
		}

		segment.Write(line)
		if hasNewline {
			segment.WriteByte('\n')
		}
	}

	flushSegment()
	contents = output.Bytes()

	return templates, contents, recurse, err
}
