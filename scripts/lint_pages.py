#!/usr/bin/env python3
"""Lint handbook pages against AUTHORING.md, GLOSSARY.md, the templates and CATALOGUE.md.

Usage:
    lint_pages.py [paths...] [--planned|--final] [--docs-dir DIR] [--category NAME]

Defaults: every docs/**/*.md except docs/_templates/** and docs/assets/**, mode --final.
A directory argument is expanded recursively (same exclusions).

Output: one line per finding  `path:line: CODE message`, then
        `lint_pages: N pages, E errors, W warnings`; exit status 1 iff errors.

Modes:
    --final    (default) every link, image and snippet target must exist on disk.
    --planned  a missing `.md` link target is fine when it is a catalogue path; a missing
               image under docs/assets/img/ is a warning (W_IMAGE) instead of an error.

--docs-dir DIR   treat DIR as the docs root when mapping pages to catalogue rows
                 (for fixtures that live outside docs/). Default: <repo>/docs.
--category NAME  force the catalogue category of the given pages (fixtures, drafts).

Standard library only; ruff-clean.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalogue  # noqa: E402

ROOT = catalogue.ROOT
DOCS = ROOT / "docs"
TEMPLATES = DOCS / "_templates"
GLOSSARY = ROOT / "GLOSSARY.md"
SITE_HOST = "hld-lld-interview-handbook.vercel.app"
EXCLUDED_DIRS = ("_templates", "assets")

CONTENT_CATEGORIES = {
    "hld-fundamental",
    "hld-case-study",
    "lld-fundamental",
    "design-pattern",
    "lld-problem",
}
CLASS_NAME_CATEGORIES = {"lld-problem", "design-pattern"}
DEEP_DIVE_PREFIX = "Deep dive: "
DEEP_DIVE_RANGE = (3, 5)
MERMAID_SOFT, MERMAID_HARD = 25, 30
MAX_INLINE_PYTHON = 12
MAX_BANG_ADMONITIONS = 3
MIN_RELATED_LINKS = 3
ALLOWED_MERMAID = (
    "flowchart LR",
    "flowchart TD",
    "flowchart TB",
    "flowchart RL",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram-v2",
    "erDiagram",
)

# --- regexes -------------------------------------------------------------------------
FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>`{3,}|~{3,})(?P<info>.*)$")
FENCE_CLOSE = re.compile(r"^[ \t]*(?P<marker>`{3,}|~{3,})[ \t]*$")
HEADING = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*$")
FRONT_KEY = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
PLACEHOLDER_CI = re.compile(
    r"\bTODO\b|\bTBD\b|\bFIXME\b|lorem ipsum|coming soon|as an AI|\[insert", re.I
)
PLACEHOLDER_CS = re.compile(r"<!-- T:|T_TITLE|T_SUBTOPIC|T_CRUX|T_TABLE|T_ONE_SENTENCE")
CODE_SPAN = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"(!?)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(\S+)")
IMG_TAG = re.compile(r"<img\b[^>]*\ssrc=\"([^\"]+)\"", re.I)
SNIPPET_INLINE = re.compile(r"--8<--\s+[\"']([^\"']+)[\"']")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
WORD = re.compile(r"[A-Za-z0-9]")
ADMONITION = re.compile(r"^\s*!!!\s+(\w+)(?:\s+\"([^\"]*)\")?")
ATTR_SUFFIX = re.compile(r"\{[^}]*\}\s*$")
SELF_LINK = re.compile(r"^https?://(www\.)?" + re.escape(SITE_HOST), re.I)
CLASS_DEF = re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.M)

# Mermaid tokenisers (heuristic node counting, see AUTHORING.md section 6).
MM_ARROW = r"<?(?:-{2,}[>ox]|-{3,}|={2,}>|={3,}|-\.+->|-\.+-|\.->|~{3,})|[ox]-{2,}[ox]"
MM_TOKEN = re.compile(
    r"(?P<q>\"[^\"]*\")"
    r"|(?P<elabel>\|[^|]*\|)"
    r"|(?P<arrow>" + MM_ARROW + r")"
    r"|(?P<lopen>(?:--|==|-\.)(?=\s))"
    r"|(?P<open>[\[({>])"
    r"|(?P<close>[\])}])"
    r"|(?P<amp>&)"
    r"|(?P<ident>[A-Za-z0-9_]+)"
    r"|(?P<other>\S)"
)
MM_FLOW_SKIP = {"flowchart", "graph", "direction", "end", "style", "classDef", "class", "click", "linkStyle"}
QUOTED = re.compile(r"\"[^\"]*\"")
CL_DECL = re.compile(r"^class\s+([A-Za-z_]\w*)")
CL_ANNOT = re.compile(r"^<<\w+>>\s+([A-Za-z_]\w*)")
CL_MEMBER = re.compile(r"^([A-Za-z_]\w*)\s*:")
CL_REL = re.compile(r"^([A-Za-z_]\w*)\s*[<|*o]*(?:--|\.\.)[>|*o]*\s*([A-Za-z_]\w*)")
CL_NOTE = re.compile(r"^note\s+for\s+([A-Za-z_]\w*)")
CL_SKIP = ("namespace", "direction", "note", "style", "classDef", "cssClass", "link", "callback", "click")
SEQ_DECL = re.compile(r"^(?:create\s+)?(?:participant|actor)\s+([A-Za-z0-9_]+)")
SEQ_MSG = re.compile(r"^([A-Za-z0-9_]+)\s*(?:<<)?-{1,2}(?:>{1,2}|[x)])\s*[+-]?\s*([A-Za-z0-9_]+)\s*:")
ST_TRANS = re.compile(r"^(\[\*\]|[A-Za-z0-9_]+)\s*-->\s*(\[\*\]|[A-Za-z0-9_]+)")
ST_DECL = re.compile(r"^state\s+(?:\"[^\"]*\"\s+as\s+)?([A-Za-z0-9_]+)")
ST_DESC = re.compile(r"^([A-Za-z0-9_]+)\s*:")
ER_REL = re.compile(r"^([A-Za-z0-9_]+)\s+[|o}{]{1,2}(?:--|\.\.)[|o}{]{1,2}\s+([A-Za-z0-9_]+)")
ER_BLOCK = re.compile(r"^([A-Za-z0-9_]+)(?:\[[^\]]*\])?\s*\{")


# --- document model --------------------------------------------------------------------
@dataclass
class Fence:
    lang: str
    start: int  # 1-based line number of the opening fence
    marker: str
    indent: str
    end: int | None = None
    body: list[str] = field(default_factory=list)  # dedented body lines


@dataclass
class Doc:
    lines: list[str]
    in_code: list[bool]  # inside a fence (delimiters included)
    in_front: list[bool]  # front matter (delimiters included)
    fences: list[Fence]
    meta: dict[str, str]
    front_error: str | None
    headings: list[tuple[int, int, str]]  # (line, level, text)

    def prose_lines(self) -> list[tuple[int, str]]:
        return [
            (i + 1, line)
            for i, line in enumerate(self.lines)
            if not self.in_code[i] and not self.in_front[i]
        ]


@dataclass(frozen=True)
class Finding:
    line: int
    code: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.code.startswith("E_")


@dataclass(frozen=True)
class Slot:
    name: str  # exact H2 text, or the prefix of a group
    group: bool = False
    optional: bool = False
    lo: int = 1
    hi: int = 1


def fence_lang(info: str) -> str:
    info = info.strip()
    if info.startswith("{"):
        for tok in info.strip("{}").split():
            if tok.startswith("."):
                return tok[1:]
        return ""
    return info.split()[0] if info else ""


def parse(text: str) -> Doc:
    lines = text.splitlines()
    n = len(lines)
    in_code = [False] * n
    in_front = [False] * n
    meta: dict[str, str] = {}
    front_error: str | None = None
    body_start = 0
    if n and lines[0].strip() == "---":
        in_front[0] = True
        j = 1
        while j < n and lines[j].strip() not in ("---", "..."):
            in_front[j] = True
            m = FRONT_KEY.match(lines[j])
            if m:
                meta[m.group(1)] = m.group(2).strip()
            j += 1
        if j < n:
            in_front[j] = True
            body_start = j + 1
        else:
            front_error = "front matter is not terminated by '---'"
            body_start = n
    else:
        front_error = "missing front matter (file must start with '---')"

    fences: list[Fence] = []
    headings: list[tuple[int, int, str]] = []
    current: Fence | None = None
    for idx in range(body_start, n):
        line = lines[idx]
        if current is not None:
            in_code[idx] = True
            m = FENCE_CLOSE.match(line)
            if (
                m
                and m.group("marker")[0] == current.marker[0]
                and len(m.group("marker")) >= len(current.marker)
            ):
                current.end = idx + 1
                fences.append(current)
                current = None
                continue
            strip = 0
            while strip < len(current.indent) and strip < len(line) and line[strip] in " \t":
                strip += 1
            current.body.append(line[strip:])
            continue
        m = FENCE_OPEN.match(line)
        if m and not (m.group("marker")[0] == "`" and "`" in m.group("info")):
            current = Fence(
                lang=fence_lang(m.group("info")),
                start=idx + 1,
                marker=m.group("marker"),
                indent=m.group("indent"),
            )
            in_code[idx] = True
            continue
        h = HEADING.match(line)
        if h and h.group("text"):
            headings.append((idx + 1, len(h.group("hashes")), h.group("text").strip()))
    if current is not None:
        fences.append(current)
    return Doc(lines, in_code, in_front, fences, meta, front_error, headings)


# --- mermaid node counting -------------------------------------------------------------
def mermaid_kind(body: list[str]) -> str | None:
    for raw in body:
        line = raw.strip()
        if line and not line.startswith("%%"):
            return line
    return None


def flowchart_nodes(body: list[str]) -> set[str]:
    nodes: set[str] = set()
    for raw in body:
        line = raw.strip()
        if not line or line.startswith("%%") or line.split()[0] in MM_FLOW_SKIP:
            continue
        toks: list[tuple[str, str | None]] = []
        depth = 0
        in_label = False
        for m in MM_TOKEN.finditer(line):
            kind = m.lastgroup or "other"
            if depth > 0:
                if kind == "open":
                    depth += 1
                elif kind == "close":
                    depth -= 1
                continue
            if kind == "open":
                depth = 1
                toks.append(("shape", None))
            elif in_label:
                if kind == "arrow":
                    in_label = False
                    toks.append(("arrow", None))
            elif kind == "lopen":
                in_label = True
            elif kind in ("arrow", "amp", "ident"):
                toks.append((kind, m.group()))
        for k, (kind, val) in enumerate(toks):
            if kind != "ident" or val is None:
                continue
            prev = toks[k - 1][0] if k else None
            nxt = toks[k + 1][0] if k + 1 < len(toks) else None
            if nxt == "shape" or prev in ("arrow", "amp") or nxt in ("arrow", "amp"):
                nodes.add(val)
    return nodes


def class_names(body: list[str]) -> set[str]:
    names: set[str] = set()
    depth = 0
    for raw in body:
        line = QUOTED.sub("", raw).strip()
        if not line or line.startswith("%%"):
            continue
        if line.startswith("}"):
            depth = max(0, depth - 1)
            continue
        if depth > 0:
            depth += line.count("{") - line.count("}")
            continue
        m = CL_NOTE.match(line)
        if m:
            names.add(m.group(1))
        elif line.startswith(CL_SKIP) and not CL_DECL.match(line):
            pass
        elif (m := CL_DECL.match(line)) or (m := CL_ANNOT.match(line)):
            names.add(m.group(1))
        elif m := CL_REL.match(line):
            names.update(m.groups())
        elif m := CL_MEMBER.match(line):
            names.add(m.group(1))
        if line.endswith("{"):
            depth += 1
    return names


def sequence_participants(body: list[str]) -> set[str]:
    names: set[str] = set()
    for raw in body:
        line = raw.strip()
        if m := SEQ_DECL.match(line):
            names.add(m.group(1))
        elif m := SEQ_MSG.match(line):
            names.update(m.groups())
    return names


def state_names(body: list[str]) -> set[str]:
    names: set[str] = set()
    for raw in body:
        line = raw.strip()
        if not line or line.startswith(("%%", "note", "direction", "stateDiagram")):
            continue
        if m := ST_TRANS.match(line):
            names.update(g for g in m.groups() if g != "[*]")
        elif m := ST_DECL.match(line):
            names.add(m.group(1))
        elif m := ST_DESC.match(line):
            names.add(m.group(1))
    return names


def er_entities(body: list[str]) -> set[str]:
    names: set[str] = set()
    for raw in body:
        line = QUOTED.sub("", raw).strip()
        if m := ER_REL.match(line):
            names.update(m.groups())
        elif (m := ER_BLOCK.match(line)) and m.group(1) != "erDiagram":
            names.add(m.group(1))
    return names


def count_nodes(first_line: str, body: list[str]) -> tuple[str, int]:
    if first_line.startswith(("flowchart", "graph")):
        return "flowchart", len(flowchart_nodes(body))
    if first_line.startswith("classDiagram"):
        return "classDiagram", len(class_names(body))
    if first_line.startswith("sequenceDiagram"):
        return "sequenceDiagram", len(sequence_participants(body))
    if first_line.startswith("stateDiagram"):
        return "stateDiagram", len(state_names(body))
    if first_line.startswith("erDiagram"):
        return "erDiagram", len(er_entities(body))
    return "diagram", 0


# --- linter ----------------------------------------------------------------------------
class Linter:
    def __init__(self, planned: bool, docs_dir: Path, category: str | None) -> None:
        self.planned = planned
        self.docs_dir = docs_dir.resolve()
        self.category_override = category
        self.pages = catalogue.load()
        self.by_path = {p.path: p for p in self.pages}
        self.catalogue_paths = set(self.by_path)
        self.category_dirs = {p.category: posixpath.dirname(p.path) for p in self.pages}
        self.banned = self._load_banned_terms()
        self._templates: dict[str, list[str]] = {}
        self._files: dict[Path, str | None] = {}

    # -- shared resources ----------------------------------------------------------------
    @staticmethod
    def _load_banned_terms() -> list[str]:
        terms: list[str] = []
        in_section = False
        for line in GLOSSARY.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                in_section = line[3:].strip().lower() == "banned terms"
                continue
            if not in_section or not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            term = cells[0] if cells else ""
            if not term or term.lower() == "banned" or set(term) <= set("-: "):
                continue
            terms.append(term)
        # longest first so "master/slave" is reported once, not also as "slave"
        return sorted(terms, key=len, reverse=True)

    def template_h2s(self, category: str) -> list[str]:
        if category not in self._templates:
            doc = parse((TEMPLATES / f"{category}.md").read_text(encoding="utf-8"))
            self._templates[category] = [t for _, lvl, t in doc.headings if lvl == 2]
        return self._templates[category]

    def read_file(self, path: Path) -> str | None:
        if path not in self._files:
            try:
                self._files[path] = path.read_text(encoding="utf-8") if path.is_file() else None
            except OSError:
                self._files[path] = None
        return self._files[path]

    def template_slots(self, category: str, slug: str) -> list[Slot]:
        h2s = self.template_h2s(category)
        slots: list[Slot] = []
        i = 0
        while i < len(h2s):
            if h2s[i].startswith(DEEP_DIVE_PREFIX):
                j = i
                while j < len(h2s) and h2s[j].startswith(DEEP_DIVE_PREFIX):
                    j += 1
                slots.append(Slot(DEEP_DIVE_PREFIX, group=True, lo=DEEP_DIVE_RANGE[0], hi=DEEP_DIVE_RANGE[1]))
                i = j
                continue
            optional = slug == "patterns-overview" or (
                category == "hld-fundamental" and h2s[i] == "Python implementation"
            )
            slots.append(Slot(h2s[i], optional=optional))
            i += 1
        # AUTHORING.md section 2: the last H2 is always "## Related", whatever the template says.
        if not any(s.name == "Related" for s in slots):
            slots.append(Slot("Related"))
        return slots

    # -- page resolution -----------------------------------------------------------------
    def logical_path(self, path: Path) -> str | None:
        """Path of the page as CATALOGUE.md spells it (docs/...), or None."""
        try:
            rel = path.resolve().relative_to(self.docs_dir)
            return "docs/" + rel.as_posix()
        except ValueError:
            pass
        if self.category_override and self.category_override in self.category_dirs:
            return posixpath.join(self.category_dirs[self.category_override], path.name)
        return None

    def page_for(self, path: Path, logical: str | None) -> catalogue.Page | None:
        if self.category_override:
            if logical in self.by_path and self.by_path[logical].category == self.category_override:
                return self.by_path[logical]
            if self.category_override not in catalogue.CATEGORIES:
                raise SystemExit(f"lint_pages: unknown category {self.category_override!r}")
            return catalogue.Page(
                id="XX-00",
                slug=path.stem,
                title="",
                path=logical or path.as_posix(),
                code=[],
                diagrams=[],
                tier="P2",
                weight="S",
                batch="-",
                links_to=[],
                scope="",
                category=self.category_override,
            )
        return self.by_path.get(logical) if logical else None

    # -- entry point ---------------------------------------------------------------------
    def lint(self, path: Path) -> list[Finding]:
        text = self.read_file(path)
        if text is None:
            return [Finding(1, "E_FILE", "file not found or unreadable")]
        doc = parse(text)
        logical = self.logical_path(path)
        page = self.page_for(path, logical)
        out: list[Finding] = []
        self.check_front_matter(doc, out)
        self.check_placeholders(doc, out)
        self.check_fences(doc, out)
        self.check_snippets(doc, out)
        self.check_links(doc, path, logical, out)
        self.check_banned_terms(doc, out)
        self.check_mermaid(doc, out)
        self.check_inline_python(doc, out)
        self.check_raw_html(doc, out)
        if page is not None:
            self.check_h2_sequence(doc, page, out)
            self.check_mermaid_min(doc, page, out)
            self.check_related(doc, page, out)
            self.check_words(doc, page, out)
            if page.category in CONTENT_CATEGORIES:
                self.check_admonitions(doc, out)
            if page.category in CLASS_NAME_CATEGORIES:
                self.check_class_names(doc, page, out)
        out.sort(key=lambda f: (f.line, f.code))
        return out

    # -- generic checks ------------------------------------------------------------------
    @staticmethod
    def check_front_matter(doc: Doc, out: list[Finding]) -> None:
        if doc.front_error:
            out.append(Finding(1, "E_FRONT_MATTER", doc.front_error))
        else:
            for key in ("title", "description"):
                if not doc.meta.get(key, "").strip("\"' "):
                    out.append(Finding(1, "E_FRONT_MATTER", f"front matter lacks a non-empty '{key}:'"))
        title = doc.meta.get("title", "").strip().strip("\"'").strip()
        h1s = [(line, text) for line, lvl, text in doc.headings if lvl == 1]
        if not h1s:
            out.append(Finding(1, "E_H1", "page has no H1 heading"))
            return
        for line, _ in h1s[1:]:
            out.append(Finding(line, "E_H1", "more than one H1 heading"))
        line, text = h1s[0]
        if title and text.strip().strip("\"'").strip() != title:
            out.append(Finding(line, "E_H1", f"H1 {text!r} does not equal front-matter title {title!r}"))

    @staticmethod
    def check_placeholders(doc: Doc, out: list[Finding]) -> None:
        for i, line in enumerate(doc.lines):
            m = PLACEHOLDER_CI.search(line) or PLACEHOLDER_CS.search(line)
            if m:
                out.append(Finding(i + 1, "E_PLACEHOLDER", f"placeholder text {m.group()!r}"))

    @staticmethod
    def check_fences(doc: Doc, out: list[Finding]) -> None:
        for f in doc.fences:
            if f.end is None:
                out.append(Finding(f.start, "E_FENCE_UNCLOSED", "code fence is never closed"))
            if not f.lang:
                out.append(Finding(f.start, "E_FENCE_LANG", "code fence without a language (use ```text for plain output)"))

    def check_snippets(self, doc: Doc, out: list[Finding]) -> None:
        for f in doc.fences:
            block: list[str] | None = None
            for k, line in enumerate(f.body):
                no = f.start + 1 + k
                stripped = line.strip()
                if stripped == "--8<--":
                    if block is None:
                        block = []
                    else:
                        for spec in block:
                            self.check_snippet_spec(spec, no, out)
                        block = None
                    continue
                if block is not None:
                    if stripped and not stripped.startswith(";"):
                        block.append(stripped)
                    continue
                for m in SNIPPET_INLINE.finditer(line):
                    self.check_snippet_spec(m.group(1), no, out)

    def check_snippet_spec(self, spec: str, line: int, out: list[Finding]) -> None:
        if spec.startswith(("http://", "https://")):
            return
        parts = spec.split(":")
        file, rest = parts[0].strip(), parts[1:]
        section = rest[0].strip() if rest and rest[0].strip() and not rest[0].strip().isdigit() else None
        target = ROOT / file
        text = self.read_file(target)
        if text is None:
            out.append(Finding(line, "E_SNIPPET", f"snippet file {file!r} does not exist (paths are relative to the repo root)"))
            return
        if section is None:
            return
        start = re.search(r"--8<--\s*\[\s*start\s*:\s*" + re.escape(section) + r"\s*\]", text)
        end = re.search(r"--8<--\s*\[\s*end\s*:\s*" + re.escape(section) + r"\s*\]", text)
        if not (start and end):
            missing = " and ".join(m for m, ok in (("[start:...]", start), ("[end:...]", end)) if not ok)
            out.append(Finding(line, "E_SNIPPET", f"section {section!r} not found in {file!r}: missing --8<-- {missing} marker"))

    def check_links(self, doc: Doc, path: Path, logical: str | None, out: list[Finding]) -> None:
        page_dir = path.resolve().parent
        logical_dir = posixpath.dirname(logical) if logical else None
        for no, raw in doc.prose_lines():
            line = CODE_SPAN.sub("", raw)
            targets: list[tuple[bool, str]] = [(bool(m.group(1)), m.group(2)) for m in LINK.finditer(line)]
            targets += [(True, m.group(1)) for m in IMG_TAG.finditer(line)]
            if m := REF_DEF.match(line):
                targets.append((False, m.group(1)))
            for is_image, target in targets:
                self.check_target(no, is_image, target, page_dir, logical_dir, out)

    def check_target(
        self,
        no: int,
        is_image: bool,
        target: str,
        page_dir: Path,
        logical_dir: str | None,
        out: list[Finding],
    ) -> None:
        code = "E_IMAGE" if is_image else "E_LINK"
        target = ATTR_SUFFIX.sub("", target).strip()
        if SELF_LINK.match(target):
            out.append(Finding(no, "E_LINK", f"absolute link to this site {target!r}; use a relative link"))
            return
        low = target.lower()
        if low.startswith(("http://", "https://", "mailto:", "#", "tel:", "data:")):
            return
        target = unquote(target.split("#", 1)[0])
        if not target:
            return
        if target.startswith("/"):
            out.append(Finding(no, code, f"absolute path {target!r}; use a relative link"))
            return
        if (page_dir / target).exists():
            return
        logical_target = posixpath.normpath(posixpath.join(logical_dir, target)) if logical_dir else None
        if is_image:
            if self.planned and logical_target and logical_target.startswith("docs/assets/img/"):
                out.append(Finding(no, "W_IMAGE", f"image {target!r} does not exist yet (planned figure)"))
            else:
                out.append(Finding(no, code, f"image {target!r} does not exist"))
            return
        if self.planned and target.endswith(".md") and logical_target in self.catalogue_paths:
            return
        hint = ""
        if target.endswith(".md") and logical_target and logical_target not in self.catalogue_paths:
            hint = " and is not a catalogue path"
        out.append(Finding(no, code, f"link target {target!r} does not exist{hint}"))

    def check_banned_terms(self, doc: Doc, out: list[Finding]) -> None:
        for no, raw in [(i + 1, line) for i, line in enumerate(doc.lines) if not doc.in_code[i]]:
            text = CODE_SPAN.sub("", raw)
            hits: list[str] = []
            for term in self.banned:
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])"
                text, n = re.subn(pattern, lambda m: " " * len(m.group()), text, flags=re.I)
                if n:
                    hits.append(term)
            if hits:
                out.append(Finding(no, "E_BANNED_TERM", "banned term(s) " + ", ".join(repr(h) for h in hits) + " (see GLOSSARY.md)"))

    @staticmethod
    def check_mermaid(doc: Doc, out: list[Finding]) -> None:
        for f in doc.fences:
            if f.lang != "mermaid":
                continue
            first = mermaid_kind(f.body)
            if first is None:
                out.append(Finding(f.start, "E_MERMAID_TYPE", "empty mermaid block"))
                continue
            if not first.startswith(ALLOWED_MERMAID):
                if first.startswith("graph"):
                    hint = "use 'flowchart LR' or 'flowchart TD' instead of 'graph'"
                elif first.startswith("stateDiagram"):
                    hint = "use 'stateDiagram-v2'"
                else:
                    hint = "allowed: " + ", ".join(ALLOWED_MERMAID)
                out.append(Finding(f.start, "E_MERMAID_TYPE", f"diagram starts with {first!r}; {hint}"))
            for k, raw in enumerate(f.body):
                line = raw.strip()
                bad = None
                if "%%{init" in line:
                    bad = "%%{init ...}%% directive"
                elif "classDef" in line:
                    bad = "classDef"
                elif re.match(r"(style|linkStyle)\s", line):
                    bad = line.split()[0] + " statement"
                elif re.match(r"click\s", line):
                    bad = "click statement"
                if bad:
                    out.append(Finding(f.start + 1 + k, "E_MERMAID_STYLE", f"{bad} is not allowed (breaks the dark theme)"))
            kind, n = count_nodes(first, f.body)
            if n > MERMAID_HARD:
                out.append(Finding(f.start, "E_MERMAID_SIZE", f"{kind} has ~{n} nodes (hard limit {MERMAID_HARD}); split it"))
            elif n > MERMAID_SOFT:
                out.append(Finding(f.start, "W_MERMAID_SIZE", f"{kind} has ~{n} nodes (soft limit {MERMAID_SOFT})"))

    @staticmethod
    def check_inline_python(doc: Doc, out: list[Finding]) -> None:
        for f in doc.fences:
            if f.lang in ("python", "py", "python3") and len(f.body) > MAX_INLINE_PYTHON:
                if not any("--8<--" in line for line in f.body):
                    out.append(Finding(f.start, "E_INLINE_PYTHON", f"inline Python block of {len(f.body)} lines (> {MAX_INLINE_PYTHON}); embed a file with --8<-- instead"))

    @staticmethod
    def check_raw_html(doc: Doc, out: list[Finding]) -> None:
        for no, raw in doc.prose_lines():
            if "<script" in CODE_SPAN.sub("", raw).lower():
                out.append(Finding(no, "E_SCRIPT", "raw <script> tags are not allowed"))

    # -- catalogue-page checks -------------------------------------------------------------
    def check_h2_sequence(self, doc: Doc, page: catalogue.Page, out: list[Finding]) -> None:
        slots = self.template_slots(page.category, page.slug)
        h2s = [(line, ATTR_SUFFIX.sub("", text).strip()) for line, lvl, text in doc.headings if lvl == 2]
        last_line = len(doc.lines) or 1
        exact = {s.name for s in slots if not s.group}
        groups = [s.name for s in slots if s.group]

        def known(text: str) -> bool:
            return text in exact or any(text.startswith(g) for g in groups)

        def err(line: int, msg: str) -> None:
            out.append(Finding(line, "E_H2_SEQUENCE", msg))

        consumed = [False] * len(h2s)
        i = 0
        for slot in slots:
            while i < len(h2s) and consumed[i]:
                i += 1
            if slot.group:
                count = 0
                while i < len(h2s) and h2s[i][1].startswith(slot.name):
                    consumed[i] = True
                    i += 1
                    count += 1
                if not slot.lo <= count <= slot.hi:
                    line = h2s[i][0] if i < len(h2s) else last_line
                    err(line, f"expected {slot.lo}-{slot.hi} consecutive '## {slot.name}...' H2s, found {count}")
                continue
            while True:
                while i < len(h2s) and consumed[i]:
                    i += 1
                if i < len(h2s) and h2s[i][1] == slot.name:
                    consumed[i] = True
                    i += 1
                    break
                if i < len(h2s) and not known(h2s[i][1]):
                    err(h2s[i][0], f"unexpected H2 '{h2s[i][1]}' (not in the {page.category} template)")
                    consumed[i] = True
                    i += 1
                    continue
                if slot.optional:
                    break
                later = next((k for k in range(i, len(h2s)) if not consumed[k] and h2s[k][1] == slot.name), None)
                if later is not None:
                    err(h2s[later][0], f"H2 '{slot.name}' is out of order (expected before '{h2s[i][1]}')")
                    consumed[later] = True
                elif i < len(h2s):
                    err(h2s[i][0], f"missing H2 '## {slot.name}' (found '## {h2s[i][1]}' instead)")
                else:
                    err(last_line, f"missing H2 '## {slot.name}' at the end of the page")
                break
        for k, (line, text) in enumerate(h2s):
            if not consumed[k]:
                suffix = " (duplicate or out of order)" if known(text) else f" (not in the {page.category} template)"
                err(line, f"unexpected H2 '{text}'{suffix}")

    @staticmethod
    def check_mermaid_min(doc: Doc, page: catalogue.Page, out: list[Finding]) -> None:
        n = sum(1 for f in doc.fences if f.lang == "mermaid")
        if n < page.mermaid_min:
            out.append(Finding(1, "E_MERMAID_MIN", f"{n} mermaid diagram(s); {page.category} pages need at least {page.mermaid_min}"))

    @staticmethod
    def check_related(doc: Doc, page: catalogue.Page, out: list[Finding]) -> None:
        h2s = [(line, text) for line, lvl, text in doc.headings if lvl == 2]
        start = next((line for line, text in h2s if text == "Related"), None)
        if start is None:
            return  # reported by the H2 sequence check
        end = next((line for line, _ in h2s if line > start), len(doc.lines) + 1)
        links = relative_md = 0
        for no, raw in doc.prose_lines():
            if start < no < end:
                for m in LINK.finditer(CODE_SPAN.sub("", raw)):
                    if m.group(1):
                        continue
                    links += 1
                    target = m.group(2).split("#", 1)[0]
                    if target.endswith(".md") and not target.lower().startswith(("http://", "https://", "/")):
                        relative_md += 1
        if relative_md < MIN_RELATED_LINKS:
            out.append(Finding(start, "E_RELATED", f"'## Related' has {relative_md} relative .md link(s) ({links} links in total); need at least {MIN_RELATED_LINKS} links to catalogue pages"))

    @staticmethod
    def check_words(doc: Doc, page: catalogue.Page, out: list[Finding]) -> None:
        words = 0
        for _, line in doc.prose_lines():
            if TABLE_SEP.match(line):
                continue
            words += sum(1 for tok in line.split() if WORD.search(tok))
        lo, hi = page.word_range
        if words < int(lo * 0.6):
            out.append(Finding(1, "E_WORDS", f"{words} prose words, far below the {page.category} target of {lo}-{hi}"))
        elif not lo <= words <= hi:
            out.append(Finding(1, "W_WORDS", f"{words} prose words, outside the {page.category} target of {lo}-{hi}"))

    @staticmethod
    def check_admonitions(doc: Doc, out: list[Finding]) -> None:
        tip = warning = bangs = 0
        for _, raw in doc.prose_lines():
            m = ADMONITION.match(raw)
            if not m:
                continue
            bangs += 1
            kind, title = m.group(1), m.group(2) or ""
            if kind == "tip" and title == "Interview tip":
                tip += 1
            elif kind == "warning" and title == "Common mistake":
                warning += 1
        if not tip:
            out.append(Finding(1, "E_ADMONITION", 'missing `!!! tip "Interview tip"` admonition'))
        if not warning:
            out.append(Finding(1, "E_ADMONITION", 'missing `!!! warning "Common mistake"` admonition'))
        if bangs > MAX_BANG_ADMONITIONS:
            out.append(Finding(1, "W_ADMONITION_COUNT", f"{bangs} `!!!` admonitions (max {MAX_BANG_ADMONITIONS}); use collapsible `???` blocks for the rest"))

    def check_class_names(self, doc: Doc, page: catalogue.Page, out: list[Finding]) -> None:
        diagram: set[str] = set()
        first_fence = None
        for f in doc.fences:
            if f.lang == "mermaid" and (mermaid_kind(f.body) or "").startswith("classDiagram"):
                diagram |= class_names(f.body)
                first_fence = first_fence or f.start
        if not diagram or not page.code:
            return
        files: list[Path] = []
        for pkg in page.packages:
            if (ROOT / pkg).is_dir():
                files += sorted((ROOT / pkg).rglob("*.py"))
        files += [ROOT / mod for mod in page.modules if (ROOT / mod).is_file()]
        if not files:
            out.append(Finding(first_fence or 1, "W_CLASS_NAMES", f"code artifacts {page.code} not found on disk; cannot verify classes {sorted(diagram)}"))
            return
        defined: set[str] = set()
        for path in files:
            defined |= set(CLASS_DEF.findall(self.read_file(path) or ""))
        missing = sorted(diagram - defined)
        if missing:
            out.append(Finding(first_fence or 1, "W_CLASS_NAMES", f"classDiagram names without a `class X` in {', '.join(page.code)}: {', '.join(missing)}"))


# --- CLI -------------------------------------------------------------------------------
def collect(args: list[str]) -> list[Path]:
    def excluded(p: Path) -> bool:
        try:
            rel = p.resolve().relative_to(DOCS.resolve())
        except ValueError:
            return False
        return bool(rel.parts) and rel.parts[0] in EXCLUDED_DIRS

    if not args:
        return sorted(p for p in DOCS.rglob("*.md") if not excluded(p))
    files: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            files += sorted(q for q in p.rglob("*.md") if not excluded(q))
        else:
            files.append(p)
    return files


def display(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="pages or directories (default: all of docs/)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--planned", action="store_true", help="links to not-yet-written catalogue pages are fine")
    mode.add_argument("--final", action="store_true", help="every target must exist (default)")
    ap.add_argument("--docs-dir", default=str(DOCS), help="directory to treat as docs/ (for fixtures)")
    ap.add_argument("--category", default=None, help="force the catalogue category of the given pages")
    ns = ap.parse_args(argv)

    linter = Linter(planned=ns.planned, docs_dir=Path(ns.docs_dir), category=ns.category)
    files = collect(ns.paths)
    errors = warnings = 0
    for path in files:
        for f in linter.lint(path):
            if f.is_error:
                errors += 1
            else:
                warnings += 1
            print(f"{display(path)}:{f.line}: {f.code} {f.message}")
    print(f"lint_pages: {len(files)} pages, {errors} errors, {warnings} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
