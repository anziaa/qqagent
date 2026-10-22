"""Incremental, source-preserving parsing of meme markers in chat text."""

import copy
import re

from .context import ContextScanner, is_escaped
from .text_safety import strip_internal_image_ref_lines
from .types import MemeToken, ParseResult


class MemeParser:
    """Parse lines incrementally while streaming unambiguous text prefixes.

    Args:
        categories: Category keys from the request's active pack.
        alternative: Recognize bracket, parenthesis and colon markers.
        loose: Recognize a category occupying its own line.
        repeated: Recognize standalone repeated category keys.
        remove_invalid: Consume invalid bracket and parenthesis markers.
        semantic: Accept semantic IDs instead of category keys.
        strip_references: Remove standalone internal image references.
        filter_all: Finally strip all marker forms without changing selections.

    Notes:
        Unconfirmed inline code is buffered until it closes or the reply ends.
        Oversized lines or pending inline spans
        and the remaining response are preserved without interpretation. This is a chat protocol scanner,
        not a complete CommonMark renderer. No network or filesystem IO occurs.
    """

    MAX_LINE = 16384
    _markers = re.compile(
        r"&&(?P<strict>[^&\r\n]{1,256})&&"
        r"|\[(?P<bracket>[^\[\]\r\n]{1,256})\]"
        r"|\((?P<paren>[^()\r\n]{1,256})\)"
        r"|(?<![\w:]):(?P<colon>[^:\s]{1,256}):(?![\w:])"
        r"|(?<![\w&])(?P<bare>(?i:meme:[0-9a-f]{12,64}))(?![\w&])"
    )

    def __init__(
        self,
        categories=(),
        *,
        alternative=False,
        loose=False,
        repeated=False,
        remove_invalid=False,
        semantic=False,
        strip_references=False,
        filter_all=False,
    ):
        self.categories = frozenset(categories)
        self.alternative = alternative
        self.loose = loose
        self.repeated = repeated
        self.remove_invalid = remove_invalid
        self.semantic = semantic
        self.strip_references = strip_references
        self.filter_all = filter_all
        self.tokens: list[MemeToken] = []
        self._line = ""
        self._emitted = 0
        self._offset = 0
        self._context = ContextScanner()
        self._pending_lines: list[str] = []
        self._pending_size = 0
        self._pending_context: ContextScanner | None = None
        self._pending_emitted = 0
        self._inline_limit = False
        self.diagnostics: list[str] = []
        self._passthrough = False
        self._finished = False

    @classmethod
    def parse(cls, text: str, categories=(), **options) -> ParseResult:
        """Parse a complete response using the identical incremental engine.

        Args:
            text: Original complete response.
            categories: Available category keys for this request.
            **options: Constructor options controlling recognized marker forms.

        Returns:
            Immutable visible text, source tokens and resource diagnostics.
        """
        parser = cls(categories, **options)
        visible = parser.feed(text) + parser.finish()
        return ParseResult(visible, tuple(parser.tokens), tuple(parser.diagnostics))

    @property
    def selections(self) -> list[str]:
        """Return validated selections in source order without duplicates."""
        return list(dict.fromkeys(token.value for token in self.tokens if token.valid))

    def feed(self, text: str) -> str:
        """Consume a text delta and return text safe to send now.

        Args:
            text: Newly received text, not the accumulated response.

        Returns:
            Visible text whose interpretation no longer needs more input.

        Raises:
            RuntimeError: If called after finish.
        """
        if self._finished:
            raise RuntimeError("Cannot feed a finished meme parser")
        output = []
        for part in text.splitlines(keepends=True):
            # 只有 LF 才能结束协议行；其他分隔符按原文保留。
            complete = part.endswith("\n")
            if self._passthrough:
                output.append(part)
                self._offset += len(part)
                continue
            self._line += part
            pending_limit = (
                self._pending_size > 0
                and self._pending_size + len(self._line) > self.MAX_LINE
            )
            if pending_limit and self.filter_all:
                # Keep final tag filtering active after abandoning inline speculation.
                self._inline_limit = True
                self.diagnostics.append("inline_code_limit_exceeded")
                output.append(self._flush_pending())
            if (
                pending_limit or len(self._line) > self.MAX_LINE
            ) and not self.filter_all:
                output.append(
                    "".join(self._pending_lines)[self._pending_emitted :]
                    + self._line[self._emitted :]
                )
                self._offset += self._pending_size + len(self._line)
                self._pending_lines.clear()
                self._pending_size = 0
                self._pending_context = None
                self._pending_emitted = 0
                self._line = ""
                self._emitted = 0
                self._passthrough = True
                self.diagnostics.append(
                    "inline_code_limit_exceeded"
                    if pending_limit
                    else "line_limit_exceeded"
                )
                continue
            if complete:
                output.append(self._consume_line())
            elif not self._context.active:
                # 任何语法出现之前，已完成的普通词不能再修改。
                # 保留缩进和可能独立出现的回退标记。
                syntax = re.search(r"[&`~\[\]()<>:\\\n]", self._line)
                prefix = self._line[: syntax.start()] if syntax else self._line
                if self.semantic or self.filter_all:
                    pending_id = re.search(r"(?i)\bm(?:e(?:m(?:e)?)?)?$", prefix)
                    if pending_id:
                        prefix = prefix[: pending_id.start()]
                if self.strip_references and re.fullmatch(
                    r"(?i)\s*f(?:i(?:l(?:e)?)?)?", prefix
                ):
                    prefix = ""
                boundary = len(prefix)
                first = prefix.strip().split(" ", 1)[0]
                fallback = self.loose or self.repeated
                ambiguous = fallback and any(
                    key.startswith(first) or first.startswith(key)
                    for key in self.categories
                )
                if (
                    boundary > self._emitted
                    and prefix.strip()
                    and not ambiguous
                    and not self._line.startswith(("    ", "\t"))
                ):
                    output.append(self._line[self._emitted : boundary])
                    self._emitted = boundary
        return "".join(output)

    def finish(self) -> str:
        """Flush the final line; incomplete markers remain literal text.

        Returns:
            Remaining visible text. Repeated calls return an empty string.
        """
        if self._finished:
            return ""
        self._finished = True
        if self._line:
            if not self._pending_lines:
                self._pending_context = copy.deepcopy(self._context)
                self._pending_emitted = self._emitted
            self._pending_lines.append(self._line)
            self._line = ""
            self._emitted = 0
        return self._flush_pending(final=not self._inline_limit)

    def _flush_pending(self, *, final: bool = False) -> str:
        """Replay deferred lines once code protection can be resolved safely.

        Args:
            final: Treat runs without a later equal-length run as literal text.

        Returns:
            Resolved text that has not already been emitted.
        """
        if not self._pending_lines:
            return ""
        lines = self._pending_lines
        emitted = self._pending_emitted
        assert self._pending_context is not None
        self._context = self._pending_context
        self._pending_lines = []
        self._pending_size = 0
        self._pending_context = None
        self._pending_emitted = 0
        literal_ticks = set()
        if final:
            text = "".join(lines)
            runs = list(ContextScanner._ticks.finditer(text))
            last_ticks = {len(match[0]): match.start() for match in runs}
            literal_ticks.update(last_ticks.values())
            for match in runs:
                # Outside code, an escape consumes only the first tick of a run.
                # Closers inside code must still match a complete raw run.
                if (
                    len(match[0]) > 1
                    and is_escaped(text, match.start())
                    and last_ticks.get(len(match[0]) - 1, -1) < match.start() + 1
                ):
                    literal_ticks.add(match.start() + 1)
        trailing_line, trailing_emitted = self._line, self._emitted
        output = []
        offset = 0
        positions = iter(sorted(literal_ticks))
        position = next(positions, None)
        for line in lines:
            line_ticks = set()
            while position is not None and position < offset + len(line):
                line_ticks.add(position - offset)
                position = next(positions, None)
            self._line, self._emitted = line, emitted
            if self._passthrough:
                output.append(line[emitted:])
                self._offset += len(line)
            else:
                output.append(self._consume_line(literal_ticks=line_ticks))
            offset += len(line)
            emitted = 0
        self._line, self._emitted = trailing_line, trailing_emitted
        return "".join(output)

    def _consume_line(self, *, literal_ticks: set[int] | None = None) -> str:
        """Resolve one bounded line and update cross-line protection state.

        Args:
            literal_ticks: Known literal backtick positions when replaying a
                resolved block. None allows unfinished inline code to be deferred.

        Returns:
            The un-emitted part of the line after recognized marker edits.
        """
        line = self._line
        checkpoint = (
            copy.deepcopy(self._context)
            if literal_ticks is None and not self._pending_lines and "`" in line
            else None
        )
        continued_context = self._context.active
        protected = self._context.scan(line, literal_ticks=literal_ticks)
        if (
            literal_ticks is None
            and not self._inline_limit
            and (self._pending_lines or self._context.ticks)
        ):
            if not self._pending_lines:
                self._pending_context = checkpoint
                self._pending_emitted = self._emitted
            self._pending_lines.append(line)
            self._pending_size += len(line)
            self._line = ""
            self._emitted = 0
            return self._flush_pending() if not self._context.ticks else ""
        if self._context.overflow and not self.filter_all:
            self._passthrough = True
            self.diagnostics.append("context_limit_exceeded")

        edits = []
        if (
            self.strip_references
            and not continued_context
            and not strip_internal_image_ref_lines(line)
        ):
            self.tokens.append(
                MemeToken(
                    self._offset, self._offset + len(line), line, "reference", "", False
                )
            )
            self._offset += len(line)
            self._line = ""
            self._emitted = 0
            return ""
        protected_index = 0
        for match in self._markers.finditer(line):
            while (
                protected_index < len(protected)
                and protected[protected_index][1] <= match.start()
            ):
                protected_index += 1
            if (
                protected_index < len(protected)
                and protected[protected_index][0] < match.end()
            ):
                continue
            if is_escaped(line, match.start()):
                continue
            kind = match.lastgroup
            if kind == "paren":
                before, after = line[: match.start()], line[match.end() :]
                if (
                    re.search(r"[a-zA-Z0-9_]$", before)
                    or re.match(r"[a-zA-Z0-9_]", after)
                    or (
                        re.search(r"[a-zA-Z]\s*$", before)
                        and re.match(r"\s*[a-zA-Z]", after)
                    )
                ):
                    continue
            value = match[kind].strip()
            if kind == "bare" and not self.semantic:
                continue
            if kind in {"strict", "bare"}:
                semantic_id = re.fullmatch(r"meme:[0-9a-f]{12,64}", value, re.I)
                valid = bool(semantic_id) if self.semantic else value in self.categories
                if semantic_id:
                    value = value.lower()
                consume = True
            elif self.alternative and not self.semantic:
                valid = value in self.categories
                consume = valid or (
                    self.remove_invalid and kind in {"bracket", "paren"}
                )
            else:
                continue
            if consume:
                edits.append((match.start(), match.end()))
                self.tokens.append(
                    MemeToken(
                        self._offset + match.start(),
                        self._offset + match.end(),
                        match[0],
                        kind,
                        value,
                        valid,
                    )
                )

        if (
            not edits
            and not self.semantic
            and not protected
            and (self.loose or self.repeated)
        ):
            word = line.strip().rstrip("。！？，、.!?,")
            value = word if self.loose and word in self.categories else ""
            if not value and self.repeated:
                value = next(
                    (
                        key
                        for key in sorted(
                            self.categories, key=lambda key: (-len(key), key)
                        )
                        if len(key) >= 2
                        and len(word) >= 2 * len(key)
                        and word == key * (len(word) // len(key))
                    ),
                    "",
                )
            if value and not line.startswith(("    ", "\t")):
                start = len(line) - len(line.lstrip())
                end = start + len(word)
                edits.append((start, end))
                self.tokens.append(
                    MemeToken(
                        self._offset + start,
                        self._offset + end,
                        word,
                        "fallback",
                        value,
                        True,
                    )
                )

        parts = []
        previous = self._emitted
        for start, end in edits:
            parts.append(line[previous:start])
            previous = end
        parts.append(line[previous:])
        self._offset += len(line)
        self._line = ""
        self._emitted = 0
        visible = "".join(parts)
        # 在常规选择之后执行清理，避免抑制有效图片。
        if self.filter_all:
            visible = self._markers.sub("", visible)
        return visible
