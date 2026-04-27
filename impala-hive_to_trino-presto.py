#!/usr/bin/env python3
"""
SQL Dialect Converter for Nashorn JavaScript and Raw SQL Files
===============================================================
Reads a dialect directive near the top of each file:

    --!impala   →  converts Impala SQL to Trino,  updates directive to --!trino
    --!hive     →  converts Hive  SQL to Presto,  updates directive to --!presto
    --!null     →  no conversion, file is left untouched

Supported file types:
    .js         →  Nashorn JS with embedded SQL in string literals
    .sql .hql .hive .ddl .dml  →  raw SQL (rules applied to entire content)

All JavaScript / Nashorn scripting is preserved exactly as-is;
only the SQL content is rewritten.

Usage:
    python impala-hive_to_trino-presto.py input.js  -o output.js
    python impala-hive_to_trino-presto.py input.sql -o output.sql
    python impala-hive_to_trino-presto.py input_dir/ -o output_dir/ --recursive
    python impala-hive_to_trino-presto.py input.sql --dry-run
    python impala-hive_to_trino-presto.py --self-test
"""

import re
import os
import sys
import shutil
import argparse
import logging
from pathlib import Path
from typing import Optional, List, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ===========================================================================
#  DATA TYPES
# ===========================================================================

class StringLiteral:
    """A JS string literal found in the source."""
    def __init__(self, start, end, quote, raw, unescaped,
                 is_sql=False, converted=None):
        self.start = start
        self.end = end
        self.quote = quote
        self.raw = raw
        self.unescaped = unescaped
        self.is_sql = is_sql
        self.converted = converted


class ConversionReport:
    def __init__(self, file, dialect="", target="",
                 strings_found=0, strings_changed=0,
                 rules_applied=None, skipped=False,
                 flags=None, udfs=None):
        self.file = file
        self.dialect = dialect
        self.target = target
        self.strings_found = strings_found
        self.strings_changed = strings_changed
        self.rules_applied = rules_applied if rules_applied is not None else []
        self.skipped = skipped
        self.flags = flags if flags is not None else []
        self.udfs = udfs if udfs is not None else []  # List of (line_num, func_name)


# ===========================================================================
#  DIALECT DETECTION
# ===========================================================================

# Matches the dialect directive in comments:  // --!impala   or  --!hive  etc.
_DIALECT_DIRECTIVE_RE = re.compile(
    r'--!(impala|hive|null|trino|presto)\b',
    re.IGNORECASE
)

# Map source dialect → (target dialect, directive to write)
DIALECT_MAP = {
    "impala": ("trino",  "--!trino"),
    "hive":   ("presto", "--!presto"),
}


def detect_dialect(source, scan_lines=30):
    """
    Scan the first *scan_lines* lines of source for a --!<dialect> directive.
    Returns the regex Match object (so we know position + dialect), or None.
    """
    # Only scan the head of the file
    lines = source.split('\n', scan_lines)
    head = '\n'.join(lines[:scan_lines])
    return _DIALECT_DIRECTIVE_RE.search(head)


# ===========================================================================
#  BALANCED-PARENTHESIS PATTERN HELPERS
# ===========================================================================
# Many SQL functions nest other function calls in their arguments, e.g.
#   TO_DATE(TRUNC(ts, 'MM'))    DATEDIFF(NOW(), TRUNC(ts, 'DD'))
#
# A naïve [^)]+ or [^,]+ breaks on the inner parentheses.  The patterns
# below match arguments with up to 2 levels of nested parens AND handle
# single-quoted strings at every level (important because TRUNC→DATE_TRUNC
# rewrites introduce quotes like DATE_TRUNC('mm', col) inside outer calls).

# Quoted string literal (SQL uses single quotes)
_Q = r"'[^']*'"

# Level 0 (innermost parens): no further nesting, but allow quoted strings.
# Pattern structure: plain_chars (special_token plain_chars)*
# This is backtrack-safe because each iteration of the repeat requires
# a mandatory special token (quote or paren) before consuming more plain chars.
_L0 = rf"[^()']*(?:{_Q}[^()']*)*"

# Level 1: one level of nested parens + quoted strings
_L1 = rf"[^()']*(?:(?:\({_L0}\)|{_Q})[^()']*)*"

# SINGLE function argument (stops at unbalanced comma).  2 nesting levels.
_ARG = rf"[^(),']*(?:(?:\({_L1}\)|{_Q})[^(),']*)*"

# FULL body inside outermost parens (commas allowed).  2 nesting levels.
_BODY = rf"[^()']*(?:(?:\({_L1}\)|{_Q})[^()']*)*"


# ===========================================================================
#  BASE CONVERTER – shared rule engine
# ===========================================================================

class SQLConverter:
    """Base class: applies an ordered list of regex rewrite rules."""

    def __init__(self, annotate=False):
        self.annotate = annotate
        self.rules = self._build_rules()

    def _build_rules(self):
        raise NotImplementedError

    def _pre_passes(self, sql):
        # type: (str) -> Tuple[str, List[str]]
        """Override in subclasses for complex rewrites that can't be done
        with simple regex sub (e.g. DECODE with variable args)."""
        return sql, []

    def convert(self, sql):
        # type: (str) -> Tuple[str, List[str]]
        applied = []
        result = sql

        # Run complex pre-passes first
        result, pre_applied = self._pre_passes(result)
        applied.extend(pre_applied)

        # Then run regex rules
        for desc, pattern, repl in self.rules:
            new = pattern.sub(repl, result) if not callable(repl) \
                  else pattern.sub(repl, result)
            if new != result:
                applied.append(desc)
                result = new
        return result, applied


# helper used by both converters
def _add(rules, desc, pattern, repl, flags=re.IGNORECASE):
    rules.append((desc, re.compile(pattern, flags), repl))


# ---------------------------------------------------------------------------
#  Complex rewrite helpers (used as callables inside rules)
# ---------------------------------------------------------------------------

def _extract_balanced_args(s):
    # type: (str) -> List[str]
    """
    Split a string by top-level commas, respecting nested parens and quotes.
    e.g. "a, FUNC(b, c), 'x,y'" → ["a", "FUNC(b, c)", "'x,y'"]
    """
    args = []
    depth = 0
    current = []
    in_quote = False

    for ch in s:
        if ch == "'" and not in_quote:
            in_quote = True
            current.append(ch)
        elif ch == "'" and in_quote:
            in_quote = False
            current.append(ch)
        elif in_quote:
            current.append(ch)
        elif ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)

    if current:
        args.append(''.join(current).strip())

    return args


def _find_balanced_parens(s, start):
    # type: (str, int) -> int
    """
    Given s and the index of an opening '(', return the index of the
    matching closing ')'.  Returns -1 if unbalanced.
    """
    depth = 0
    in_quote = False
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "'" and not in_quote:
            in_quote = True
        elif ch == "'" and in_quote:
            in_quote = False
        elif not in_quote:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _decode_to_case(m):
    """
    Convert DECODE(expr, search1, result1 [, search2, result2 ...] [, default])
    to CASE WHEN expr = search1 THEN result1 ... ELSE default END.
    """
    # m matched \bDECODE\s*\(  — we need to find the full arg list
    # Since regex can't handle variable args, we find the closing paren
    # from the original match, then parse the args.
    full = m.string
    open_idx = m.end() - 1  # index of the '('
    close_idx = _find_balanced_parens(full, open_idx)
    if close_idx == -1:
        return m.group(0)  # can't parse, leave as-is

    inner = full[open_idx + 1:close_idx]
    args = _extract_balanced_args(inner)

    if len(args) < 3:
        return m.group(0)  # not enough args, leave as-is

    expr = args[0]
    parts = ["CASE"]

    # Pairs of (search, result)
    i = 1
    while i + 1 < len(args):
        parts.append(f"WHEN {expr} = {args[i]} THEN {args[i+1]}")
        i += 2

    # If there's an odd remaining arg, it's the default
    if i < len(args):
        parts.append(f"ELSE {args[i]}")

    parts.append("END")

    # We need to return the replacement for the entire DECODE(...) region.
    # But the regex only matched up to the '(' — the rest of the match
    # extends beyond what the regex captured.  We use a trick: return
    # the CASE expression plus a marker to tell the caller how much
    # of the original string was consumed.  Since regex sub can't do this,
    # we'll handle DECODE as a special pre-pass instead.
    return ' '.join(parts)


def _decode_prepass(sql):
    # type: (str) -> Tuple[str, bool]
    """
    Pre-processing pass: find all DECODE(...) calls and convert to CASE.
    Returns (converted_sql, changed).
    """
    pattern = re.compile(r'\bDECODE\s*\(', re.IGNORECASE)
    result = sql
    changed = False

    # Process from right to left so offsets stay valid
    matches = list(pattern.finditer(result))
    for m in reversed(matches):
        open_idx = m.end() - 1
        close_idx = _find_balanced_parens(result, open_idx)
        if close_idx == -1:
            continue

        inner = result[open_idx + 1:close_idx]
        args = _extract_balanced_args(inner)

        if len(args) < 3:
            continue

        expr = args[0]
        parts = ["CASE"]
        i = 1
        while i + 1 < len(args):
            parts.append(f"WHEN {expr} = {args[i]} THEN {args[i+1]}")
            i += 2
        if i < len(args):
            parts.append(f"ELSE {args[i]}")
        parts.append("END")

        case_expr = ' '.join(parts)
        result = result[:m.start()] + case_expr + result[close_idx + 1:]
        changed = True

    return result, changed


# Java-style → MySQL-style date format specifier mapping
_JAVA_TO_MYSQL_FMT = {
    'yyyy': '%Y', 'yy': '%y',
    'MM': '%m', 'M': '%c',
    'dd': '%d', 'd': '%e',
    'HH': '%H', 'hh': '%h', 'H': '%k', 'h': '%l',
    'mm': '%i',
    'ss': '%s', 'S': '%f',
    'a': '%p',      # AM/PM
    'EEE': '%a',    # abbreviated day name
    'EEEE': '%W',   # full day name
    'MMM': '%b',    # abbreviated month name
    'MMMM': '%M',   # full month name
}

# Build a regex that matches the longest specifiers first
_JAVA_FMT_RE = re.compile(
    '|'.join(re.escape(k) for k in
             sorted(_JAVA_TO_MYSQL_FMT.keys(), key=len, reverse=True))
)


def _convert_java_to_mysql_format(fmt_str: str) -> str:
    """Convert a Java-style date format string to MySQL/Trino style."""
    return _JAVA_FMT_RE.sub(lambda m: _JAVA_TO_MYSQL_FMT[m.group(0)], fmt_str)


# ===========================================================================
#  IMPALA → TRINO  RULES
# ===========================================================================

class ImpalaToTrinoConverter(SQLConverter):

    def _pre_passes(self, sql):
        """Handle DECODE→CASE which requires balanced-paren parsing."""
        applied = []
        result, changed = _decode_prepass(sql)
        if changed:
            applied.append("DECODE(...) → CASE WHEN ... END")
        return result, applied

    def _build_rules(self):
        r = []
        ann = self.annotate  # shorthand

        # ── Impala-only statements ─────────────────────────────────────
        _add(r, "Remove COMPUTE STATS",
             r"(?m)^\s*COMPUTE\s+(?:INCREMENTAL\s+)?STATS\s+[^;]*;?\s*$",
             "/* REMOVED: COMPUTE STATS (not needed in Trino) */" if ann else "")
        _add(r, "Remove INVALIDATE METADATA",
             r"(?m)^\s*INVALIDATE\s+METADATA\s*[^;]*;?\s*$",
             "/* REMOVED: INVALIDATE METADATA (not needed in Trino) */" if ann else "")
        _add(r, "Remove REFRESH",
             r"(?m)^\s*REFRESH\s+[^;]*;?\s*$",
             "/* REMOVED: REFRESH (not needed in Trino) */" if ann else "")

        # ── Data types ─────────────────────────────────────────────────
        _add(r, "CAST(... AS STRING) → CAST(... AS VARCHAR)",
             r"\bCAST\s*\(\s*(" + _BODY + r")\s+AS\s+STRING\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS VARCHAR)")
        _add(r, "Type STRING → VARCHAR",
             r"(?<=[\s,(])STRING(?=[\s,)])", "VARCHAR")
        _add(r, "Type TINYINT → SMALLINT",
             r"\bTINYINT\b", "SMALLINT")
        _add(r, "Type FLOAT → REAL",
             r"\bFLOAT\b", "REAL")
        _add(r, "Type INT → INTEGER",
             r"\bINT\b(?!EGER|ERVAL|O\b)", "INTEGER")

        # ── Storage / DDL clauses ──────────────────────────────────────
        _add(r, "STORED AS fmt → WITH (format = 'fmt')",
             r"\bSTORED\s+AS\s+(PARQUET|PARQUETFILE|TEXTFILE|RCFILE|"
             r"SEQUENCEFILE|AVRO|ORC|JSONFILE)\b",
             lambda m: (
                 ("/* Was: {} */ ".format(m.group(0)) if ann else "") +
                 "WITH (format = '{}')".format(
                     m.group(1).upper()
                     .replace('PARQUETFILE', 'PARQUET')
                     .replace('JSONFILE', 'JSON')
                     .replace('SEQUENCEFILE', 'SEQUENCEFILE')
                 )))
        _add(r, "Remove ROW FORMAT DELIMITED ...",
             r"\bROW\s+FORMAT\s+DELIMITED\s+FIELDS\s+TERMINATED\s+BY\s+'[^']*'"
             r"(?:\s+LINES\s+TERMINATED\s+BY\s+'[^']*')?",
             "/* \\g<0> – adjust for Trino connector */" if ann else "")
        _add(r, "Remove LOCATION",
             r"\bLOCATION\s+'[^']*'",
             "/* \\g<0> – adjust for Trino connector */" if ann else "")
        _add(r, "Remove TBLPROPERTIES",
             r"\bTBLPROPERTIES\s*\([^)]*\)",
             "/* \\g<0> – adjust for Trino connector */" if ann else "")
        _add(r, "Remove SORT BY",
             r"\bSORT\s+BY\s*\([^)]*\)",
             "/* \\g<0> – adjust for Trino connector */" if ann else "")

        # ── INSERT OVERWRITE ───────────────────────────────────────────
        _add(r, "INSERT OVERWRITE → INSERT INTO",
             r"\bINSERT\s+OVERWRITE\s+(?:TABLE\s+)?(\S+)",
             r"/* Was INSERT OVERWRITE – truncate first in Trino */ INSERT INTO \1"
             if ann else r"INSERT INTO \1")

        # ── Functions ──────────────────────────────────────────────────
        _add(r, "NVL → COALESCE",
             r"\bNVL\s*\(", "COALESCE(")
        _add(r, "NVL2(e,a,b) → IF(e IS NOT NULL, a, b)",
             r"\bNVL2\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"IF({m.group(1).strip()} IS NOT NULL, "
                        f"{m.group(2).strip()}, {m.group(3).strip()})")
        _add(r, "IFNULL → COALESCE",
             r"\bIFNULL\s*\(", "COALESCE(")
        _add(r, "GROUP_CONCAT → ARRAY_JOIN(ARRAY_AGG(...))",
             r"\bGROUP_CONCAT\s*\(\s*(" + _ARG + r")\s*(?:,\s*'([^']*)'\s*)?\)",
             lambda m: f"ARRAY_JOIN(ARRAY_AGG({m.group(1).strip()}), "
                        f"'{m.group(2) if m.group(2) else ','}')")

        # Date / time
        _add(r, "UNIX_TIMESTAMP() → TO_UNIXTIME(current_timestamp)",
             r"\bUNIX_TIMESTAMP\s*\(\s*\)", "TO_UNIXTIME(current_timestamp)")
        _add(r, "UNIX_TIMESTAMP(expr) → TO_UNIXTIME(",
             r"\bUNIX_TIMESTAMP\s*\(", "TO_UNIXTIME(")
        _add(r, "FROM_TIMESTAMP → DATE_FORMAT",
             r"\bFROM_TIMESTAMP\s*\(", "DATE_FORMAT(")
        _add(r, "TO_DATE(expr) → CAST(expr AS DATE)",
             r"\bTO_DATE\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS DATE)")
        _add(r, "DATEDIFF(a,b) → DATE_DIFF('day', b, a)",
             r"\bDATEDIFF\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"DATE_DIFF('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_ADD(d, n) → DATE_ADD('day', n, d)",
             r"\bDATE_ADD\s*\(\s*(" + _ARG + r")\s*,\s*(?:INTERVAL\s+)?(" + _ARG + r")\s*\)",
             lambda m: f"DATE_ADD('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_SUB(d, n) → DATE_ADD('day', -n, d)",
             r"\bDATE_SUB\s*\(\s*(" + _ARG + r")\s*,\s*(?:INTERVAL\s+)?(" + _ARG + r")\s*\)",
             lambda m: f"DATE_ADD('day', -({m.group(2).strip()}), {m.group(1).strip()})")
        _add(r, "ADD_MONTHS(d, n) → DATE_ADD('month', n, d)",
             r"\bADD_MONTHS\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"DATE_ADD('month', {m.group(2).strip()}, {m.group(1).strip()})")
        # Impala *_ADD / *_SUB family: MONTHS_ADD, MONTHS_SUB, DAYS_ADD, etc.
        for _unit in ['year', 'month', 'week', 'day', 'hour', 'minute', 'second']:
            _U = _unit.upper()
            _pl = _U + 'S'  # YEARS, MONTHS, WEEKS, ...
            _add(r, f"{_pl}_ADD(d, n) → DATE_ADD('{_unit}', n, d)",
                 r"\b" + _pl + r"_ADD\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
                 lambda m, u=_unit: f"DATE_ADD('{u}', {m.group(2).strip()}, {m.group(1).strip()})")
            _add(r, f"{_pl}_SUB(d, n) → DATE_ADD('{_unit}', -n, d)",
                 r"\b" + _pl + r"_SUB\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
                 lambda m, u=_unit: f"DATE_ADD('{u}', -({m.group(2).strip()}), {m.group(1).strip()})")
        _add(r, "MONTHS_BETWEEN(a,b) → DATE_DIFF('month', b, a)",
             r"\bMONTHS_BETWEEN\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"DATE_DIFF('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "TRUNC(d, fmt) → DATE_TRUNC(fmt, d)",
             r"\bTRUNC\s*\(\s*(" + _ARG + r")\s*,\s*'([^']+)'\s*\)",
             lambda m: f"DATE_TRUNC('{m.group(2).strip().lower()}', {m.group(1).strip()})")
        _add(r, "CURRENT_TIMESTAMP() → current_timestamp",
             r"\bCURRENT_TIMESTAMP\s*\(\s*\)", "current_timestamp")
        _add(r, "NOW() → current_timestamp",
             r"\bNOW\s*\(\s*\)", "current_timestamp")

        # String
        _add(r, "STRLEFT(s,n) → SUBSTR(s,1,n)",
             r"\bSTRLEFT\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"SUBSTR({m.group(1).strip()}, 1, {m.group(2).strip()})")
        _add(r, "STRRIGHT(s,n) → SUBSTR(s,-n)",
             r"\bSTRRIGHT\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"SUBSTR({m.group(1).strip()}, -{m.group(2).strip()})")
        _add(r, "INSTR → STRPOS",
             r"\bINSTR\s*\(", "STRPOS(")
        _add(r, "LCASE → lower",
             r"\bLCASE\s*\(", "lower(")
        _add(r, "UCASE → upper",
             r"\bUCASE\s*\(", "upper(")
        _add(r, "levenshtein → levenshtein_distance",
             r"\bLEVENSHTEIN\s*\(", "levenshtein_distance(")
        _add(r, "BASE64ENCODE → to_base64",
             r"\bBASE64ENCODE\s*\(", "to_base64(")
        _add(r, "BASE64DECODE → from_base64",
             r"\bBASE64DECODE\s*\(", "from_base64(")
        _add(r, "FIND_IN_SET → flag no equivalent",
             r"\bFIND_IN_SET\s*\(",
             "/* TODO: FIND_IN_SET has no direct Trino equivalent – rewrite with STRPOS or ARRAY */ FIND_IN_SET(")

        # Math / hash
        _add(r, "FNV_HASH → XXHASH64",
             r"\bFNV_HASH\s*\(", "XXHASH64(")
        _add(r, "PMOD(a,b) → ((a%b)+b)%b",
             r"\bPMOD\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"(({m.group(1).strip()} % {m.group(2).strip()}) "
                        f"+ {m.group(2).strip()}) % {m.group(2).strip()}")

        # Window functions / analytics
        _add(r, "NDV(col) → APPROX_DISTINCT(col)",
             r"\bNDV\s*\(", "APPROX_DISTINCT(")
        _add(r, "APPX_MEDIAN(col) → APPROX_PERCENTILE(col, 0.5)",
             r"\bAPPX_MEDIAN\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"APPROX_PERCENTILE({m.group(1).strip()}, 0.5)")
        _add(r, "ANALYTIC: FIRST_VALUE IGNORE NULLS → FIRST_VALUE ... IGNORE NULLS (same syntax)",
             r"placeholder_fv_xyzzy", "")  # same in Trino, doc-only
        _add(r, "GROUP_CONCAT in window → ARRAY_JOIN(ARRAY_AGG(...) OVER ...)",
             r"placeholder_gc_window_xyzzy", "")  # handled by GROUP_CONCAT rule above

        # ── REGEXP / RLIKE / ILIKE / IREGEXP operators ─────────────────
        _add(r, "col REGEXP 'pat' → REGEXP_LIKE(col, 'pat')",
             r"\b(\S+)\s+REGEXP\s+'([^']+)'",
             lambda m: f"REGEXP_LIKE({m.group(1).strip()}, '{m.group(2)}')")
        _add(r, "col RLIKE 'pat' → REGEXP_LIKE(col, 'pat')",
             r"\b(\S+)\s+RLIKE\s+'([^']+)'",
             lambda m: f"REGEXP_LIKE({m.group(1).strip()}, '{m.group(2)}')")
        _add(r, "col ILIKE 'pat' → lower(col) LIKE lower('pat')",
             r"\b(\S+)\s+ILIKE\s+'([^']+)'",
             lambda m: f"lower({m.group(1).strip()}) LIKE lower('{m.group(2)}')")
        _add(r, "col IREGEXP 'pat' → regexp_like(col, '(?i)pat')",
             r"\b(\S+)\s+IREGEXP\s+'([^']+)'",
             lambda m: f"regexp_like({m.group(1).strip()}, '(?i){m.group(2)}')")

        # ── Null-handling functions ────────────────────────────────────
        _add(r, "NULLIFZERO(x) → NULLIF(x, 0)",
             r"\bNULLIFZERO\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"NULLIF({m.group(1).strip()}, 0)")
        _add(r, "ZEROIFNULL(x) → COALESCE(x, 0)",
             r"\bZEROIFNULL\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"COALESCE({m.group(1).strip()}, 0)")
        _add(r, "ISNULL(x) → (x IS NULL)  [predicate form]",
             r"\bISNULL\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"({m.group(1).strip()} IS NULL)")

        # ── Timezone functions ─────────────────────────────────────────
        _add(r, "from_utc_timestamp(ts, tz) → AT TIME ZONE",
             r"\bFROM_UTC_TIMESTAMP\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS TIMESTAMP) AT TIME ZONE {m.group(2).strip()}")
        _add(r, "to_utc_timestamp(ts, tz) → AT TIME ZONE 'UTC'",
             r"\bTO_UTC_TIMESTAMP\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AT TIME ZONE {m.group(2).strip()} AS TIMESTAMP) AT TIME ZONE 'UTC'")

        # ── Semi/Anti joins (rewrite to EXISTS/NOT EXISTS) ─────────────
        # These can't be auto-converted perfectly for all cases, but the
        # common pattern  SELECT ... FROM t1 LEFT SEMI JOIN t2 ON cond
        # can be flagged clearly.
        _add(r, "LEFT SEMI JOIN → flag for EXISTS rewrite",
             r"\bLEFT\s+SEMI\s+JOIN\b",
             "/* TODO: Rewrite LEFT SEMI JOIN to WHERE EXISTS (...) */ JOIN")
        _add(r, "RIGHT SEMI JOIN → flag for EXISTS rewrite",
             r"\bRIGHT\s+SEMI\s+JOIN\b",
             "/* TODO: Rewrite RIGHT SEMI JOIN to WHERE EXISTS (...) */ JOIN")
        _add(r, "LEFT ANTI JOIN → flag for NOT EXISTS rewrite",
             r"\bLEFT\s+ANTI\s+JOIN\b",
             "/* TODO: Rewrite LEFT ANTI JOIN to WHERE NOT EXISTS (...) or LEFT JOIN ... WHERE key IS NULL */ LEFT JOIN")
        _add(r, "RIGHT ANTI JOIN → flag for NOT EXISTS rewrite",
             r"\bRIGHT\s+ANTI\s+JOIN\b",
             "/* TODO: Rewrite RIGHT ANTI JOIN to WHERE NOT EXISTS (...) */ RIGHT JOIN")

        # ── Date format specifier conversion (Java→MySQL style) ───────
        # Targets format strings inside date_format() and date_parse()
        _add(r, "date_format/date_parse format: Java→MySQL specifiers",
             r"\b(DATE_FORMAT|DATE_PARSE)\s*\(\s*(" + _ARG + r")\s*,\s*'([^']+)'\s*\)",
             lambda m: (f"{m.group(1)}({m.group(2).strip()}, "
                        f"'{_convert_java_to_mysql_format(m.group(3))}')"))

        # ── System / session functions ──────────────────────────────────
        # Trino uses bare keywords (no parens) for these
        _add(r, "effective_user() → current_user",
             r"\bEFFECTIVE_USER\s*\(\s*\)", "current_user")
        _add(r, "logged_in_user() → current_user",
             r"\bLOGGED_IN_USER\s*\(\s*\)", "current_user")
        # USER() as a function call (not as a keyword in other contexts)
        _add(r, "USER() → current_user",
             r"\bUSER\s*\(\s*\)", "current_user")
        _add(r, "current_database() → current_schema",
             r"\bCURRENT_DATABASE\s*\(\s*\)", "current_schema")
        # Impala-only functions with no Trino equivalent
        _add(r, "pid() → flag no equivalent",
             r"\bPID\s*\(\s*\)",
             "/* TODO: pid() has no Trino equivalent – remove or replace */ pid()")
        _add(r, "coordinator() → flag no equivalent",
             r"\bCOORDINATOR\s*\(\s*\)",
             "/* TODO: coordinator() has no Trino equivalent – remove or replace */ coordinator()")
        _add(r, "sleep(n) → flag no equivalent",
             r"\bSLEEP\s*\(",
             "/* TODO: sleep() has no Trino equivalent – remove or replace */ sleep(")

        # Identifiers / hints / misc
        _add(r, "Backtick identifiers → double-quotes",
             r"`([^`]+)`", r'"\1"')
        _add(r, "Remove Impala hints",
             r"/\*\s*\+\s*(?:SHUFFLE|NOSHUFFLE|BROADCAST|STRAIGHT_JOIN)\s*\*/", "")
        _add(r, "SHOW DATABASES → SHOW SCHEMAS",
             r"\bSHOW\s+DATABASES\b", "SHOW SCHEMAS")
        _add(r, "TABLESAMPLE SYSTEM → BERNOULLI",
             r"\bTABLESAMPLE\s+SYSTEM\s*\(", "TABLESAMPLE BERNOULLI(")

        return r


# ===========================================================================
#  HIVE → PRESTO  RULES
# ===========================================================================

class HiveToPrestoConverter(SQLConverter):

    def _pre_passes(self, sql):
        """Handle DECODE→CASE which requires balanced-paren parsing."""
        applied = []
        result, changed = _decode_prepass(sql)
        if changed:
            applied.append("DECODE(...) → CASE WHEN ... END")
        return result, applied

    def _build_rules(self):
        r = []
        ann = self.annotate  # shorthand

        # ── Hive-only statements ───────────────────────────────────────
        _add(r, "Remove MSCK REPAIR TABLE",
             r"(?m)^\s*MSCK\s+REPAIR\s+TABLE\s+[^;]*;?\s*$",
             "/* REMOVED: MSCK REPAIR TABLE (not needed in Presto) */" if ann else "")
        _add(r, "Remove ANALYZE TABLE",
             r"(?m)^\s*ANALYZE\s+TABLE\s+[^;]*;?\s*$",
             "/* REMOVED: ANALYZE TABLE (not needed in Presto) */" if ann else "")

        # ── Data types ─────────────────────────────────────────────────
        _add(r, "CAST(... AS STRING) → CAST(... AS VARCHAR)",
             r"\bCAST\s*\(\s*(" + _BODY + r")\s+AS\s+STRING\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS VARCHAR)")
        _add(r, "Type STRING → VARCHAR",
             r"(?<=[\s,(])STRING(?=[\s,)])", "VARCHAR")
        _add(r, "Type TINYINT → SMALLINT",
             r"\bTINYINT\b", "SMALLINT")
        _add(r, "Type FLOAT → REAL",
             r"\bFLOAT\b", "REAL")
        _add(r, "Type INT → INTEGER",
             r"\bINT\b(?!EGER|ERVAL|O\b)", "INTEGER")
        _add(r, "Type BINARY → VARBINARY",
             r"\bBINARY\b", "VARBINARY")

        # ── Storage / DDL clauses ──────────────────────────────────────
        _add(r, "STORED AS fmt → WITH (format = 'fmt')",
             r"\bSTORED\s+AS\s+(PARQUET|PARQUETFILE|TEXTFILE|RCFILE|"
             r"SEQUENCEFILE|AVRO|ORC|JSONFILE)\b",
             lambda m: (
                 ("/* Was: {} */ ".format(m.group(0)) if ann else "") +
                 "WITH (format = '{}')".format(
                     m.group(1).upper()
                     .replace('PARQUETFILE', 'PARQUET')
                     .replace('JSONFILE', 'JSON')
                     .replace('SEQUENCEFILE', 'SEQUENCEFILE')
                 )))
        _add(r, "Remove ROW FORMAT DELIMITED ...",
             r"\bROW\s+FORMAT\s+DELIMITED\s+FIELDS\s+TERMINATED\s+BY\s+'[^']*'"
             r"(?:\s+LINES\s+TERMINATED\s+BY\s+'[^']*')?",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")
        _add(r, "Remove ROW FORMAT SERDE ...",
             r"\bROW\s+FORMAT\s+SERDE\s+'[^']*'(?:\s+WITH\s+SERDEPROPERTIES\s*\([^)]*\))?",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")
        _add(r, "Remove LOCATION",
             r"\bLOCATION\s+'[^']*'",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")
        _add(r, "Remove TBLPROPERTIES",
             r"\bTBLPROPERTIES\s*\([^)]*\)",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")
        _add(r, "Remove CLUSTERED BY ... INTO n BUCKETS",
             r"\bCLUSTERED\s+BY\s*\([^)]*\)\s*(?:SORTED\s+BY\s*\([^)]*\)\s*)?"
             r"INTO\s+\d+\s+BUCKETS",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")
        _add(r, "Remove SORT BY",
             r"\bSORT\s+BY\s*\([^)]*\)",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")
        _add(r, "Remove DISTRIBUTE BY",
             r"\bDISTRIBUTE\s+BY\b[^;)]*",
             "/* \\g<0> – adjust for Presto connector */" if ann else "")

        # ── INSERT OVERWRITE ───────────────────────────────────────────
        _add(r, "INSERT OVERWRITE → INSERT INTO",
             r"\bINSERT\s+OVERWRITE\s+(?:TABLE\s+)?(\S+)",
             r"/* Was INSERT OVERWRITE – truncate first in Presto */ INSERT INTO \1"
             if ann else r"INSERT INTO \1")

        # ── Functions ──────────────────────────────────────────────────
        _add(r, "NVL → COALESCE",
             r"\bNVL\s*\(", "COALESCE(")
        _add(r, "IFNULL → COALESCE",
             r"\bIFNULL\s*\(", "COALESCE(")

        # Collection functions
        _add(r, "COLLECT_LIST → ARRAY_AGG",
             r"\bCOLLECT_LIST\s*\(", "ARRAY_AGG(")
        _add(r, "COLLECT_SET → SET_AGG",
             r"\bCOLLECT_SET\s*\(", "SET_AGG(")
        _add(r, "SIZE(collection) → CARDINALITY(collection)",
             r"\bSIZE\s*\(", "CARDINALITY(")
        _add(r, "SORT_ARRAY → ARRAY_SORT",
             r"\bSORT_ARRAY\s*\(", "ARRAY_SORT(")
        _add(r, "ARRAY_CONTAINS(arr,val) → CONTAINS(arr,val)",
             r"\bARRAY_CONTAINS\s*\(", "CONTAINS(")

        # String
        _add(r, "INSTR → STRPOS",
             r"\bINSTR\s*\(", "STRPOS(")
        _add(r, "LOCATE(needle,hay) → STRPOS(hay,needle)",
             r"\bLOCATE\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"STRPOS({m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "LCASE → lower",
             r"\bLCASE\s*\(", "lower(")
        _add(r, "UCASE → upper",
             r"\bUCASE\s*\(", "upper(")
        _add(r, "levenshtein → levenshtein_distance",
             r"\bLEVENSHTEIN\s*\(", "levenshtein_distance(")
        _add(r, "FIND_IN_SET → flag no equivalent",
             r"\bFIND_IN_SET\s*\(",
             "/* TODO: FIND_IN_SET has no direct Presto equivalent – rewrite with STRPOS or ARRAY */ FIND_IN_SET(")

        # JSON
        _add(r, "GET_JSON_OBJECT → json_extract_scalar",
             r"\bGET_JSON_OBJECT\s*\(", "json_extract_scalar(")

        # Hash
        _add(r, "SHA(s) → sha1(s)",
             r"\bSHA\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"sha1({m.group(1).strip()})")
        _add(r, "SHA2(s, 256) → sha256(s) / SHA2(s, 512) → sha512(s)",
             r"\bSHA2\s*\(\s*(" + _ARG + r")\s*,\s*(\d+)\s*\)",
             lambda m: ("sha256({})".format(m.group(1).strip()) if m.group(2).strip() == '256'
                        else "sha512({})".format(m.group(1).strip()) if m.group(2).strip() == '512'
                        else "/* TODO: SHA2 with length {} not supported */ SHA2({}, {})".format(
                            m.group(2).strip(), m.group(1).strip(), m.group(2).strip())))

        # Encoding
        _add(r, "BASE64 → to_base64",
             r"\bBASE64\s*\(", "to_base64(")
        _add(r, "UNBASE64 → from_base64",
             r"\bUNBASE64\s*\(", "from_base64(")

        # Regex
        _add(r, "RLIKE → REGEXP_LIKE",
             r"\b(\S+)\s+RLIKE\s+'([^']+)'",
             lambda m: f"REGEXP_LIKE({m.group(1).strip()}, '{m.group(2)}')")

        # Date / time
        _add(r, "UNIX_TIMESTAMP() → TO_UNIXTIME(current_timestamp)",
             r"\bUNIX_TIMESTAMP\s*\(\s*\)", "TO_UNIXTIME(current_timestamp)")
        _add(r, "UNIX_TIMESTAMP(expr) → TO_UNIXTIME(",
             r"\bUNIX_TIMESTAMP\s*\(", "TO_UNIXTIME(")
        _add(r, "TO_DATE(expr) → CAST(expr AS DATE)",
             r"\bTO_DATE\s*\(\s*(" + _BODY + r")\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS DATE)")
        _add(r, "DATEDIFF(a,b) → DATE_DIFF('day', b, a)",
             r"\bDATEDIFF\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"DATE_DIFF('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_ADD(d, n) → DATE_ADD('day', n, d)",
             r"\bDATE_ADD\s*\(\s*(" + _ARG + r")\s*,\s*(?:INTERVAL\s+)?(" + _ARG + r")\s*\)",
             lambda m: f"DATE_ADD('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_SUB(d, n) → DATE_ADD('day', -n, d)",
             r"\bDATE_SUB\s*\(\s*(" + _ARG + r")\s*,\s*(?:INTERVAL\s+)?(" + _ARG + r")\s*\)",
             lambda m: f"DATE_ADD('day', -({m.group(2).strip()}), {m.group(1).strip()})")
        _add(r, "ADD_MONTHS(d, n) → DATE_ADD('month', n, d)",
             r"\bADD_MONTHS\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"DATE_ADD('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "MONTHS_BETWEEN(a,b) → DATE_DIFF('month', b, a)",
             r"\bMONTHS_BETWEEN\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"DATE_DIFF('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "TRUNC(d, fmt) → DATE_TRUNC(fmt, d)",
             r"\bTRUNC\s*\(\s*(" + _ARG + r")\s*,\s*'([^']+)'\s*\)",
             lambda m: f"DATE_TRUNC('{m.group(2).strip().lower()}', {m.group(1).strip()})")
        _add(r, "CURRENT_TIMESTAMP() → current_timestamp",
             r"\bCURRENT_TIMESTAMP\s*\(\s*\)", "current_timestamp")
        _add(r, "NOW() → current_timestamp",
             r"\bNOW\s*\(\s*\)", "current_timestamp")

        # Aggregate / analytic
        _add(r, "PERCENTILE_APPROX → APPROX_PERCENTILE",
             r"\bPERCENTILE_APPROX\s*\(", "APPROX_PERCENTILE(")

        # Math
        _add(r, "PMOD(a,b) → ((a%b)+b)%b",
             r"\bPMOD\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"(({m.group(1).strip()} % {m.group(2).strip()}) "
                        f"+ {m.group(2).strip()}) % {m.group(2).strip()}")

        # LATERAL VIEW EXPLODE → CROSS JOIN UNNEST
        _add(r, "LATERAL VIEW EXPLODE(col) t AS c → CROSS JOIN UNNEST(col) AS t(c)",
             r"\bLATERAL\s+VIEW\s+EXPLODE\s*\(\s*(" + _BODY + r")\s*\)\s+(\w+)\s+AS\s+(\w+)",
             lambda m: f"CROSS JOIN UNNEST({m.group(1).strip()}) "
                        f"AS {m.group(2).strip()}({m.group(3).strip()})")
        _add(r, "LATERAL VIEW POSEXPLODE(col) t AS p,c → CROSS JOIN UNNEST(col) WITH ORDINALITY AS t(c,p)",
             r"\bLATERAL\s+VIEW\s+POSEXPLODE\s*\(\s*(" + _BODY + r")\s*\)\s+(\w+)\s+AS\s+(\w+)\s*,\s*(\w+)",
             lambda m: f"CROSS JOIN UNNEST({m.group(1).strip()}) WITH ORDINALITY "
                        f"AS {m.group(2).strip()}({m.group(4).strip()}, {m.group(3).strip()})")

        # ── Timezone functions ─────────────────────────────────────────
        _add(r, "from_utc_timestamp(ts, tz) → AT TIME ZONE",
             r"\bFROM_UTC_TIMESTAMP\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS TIMESTAMP) AT TIME ZONE {m.group(2).strip()}")
        _add(r, "to_utc_timestamp(ts, tz) → AT TIME ZONE 'UTC'",
             r"\bTO_UTC_TIMESTAMP\s*\(\s*(" + _ARG + r")\s*,\s*(" + _ARG + r")\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AT TIME ZONE {m.group(2).strip()} AS TIMESTAMP) AT TIME ZONE 'UTC'")

        # ── Semi/Anti joins ────────────────────────────────────────────
        _add(r, "LEFT SEMI JOIN → flag for EXISTS rewrite",
             r"\bLEFT\s+SEMI\s+JOIN\b",
             "/* TODO: Rewrite LEFT SEMI JOIN to WHERE EXISTS (...) */ JOIN")
        _add(r, "LEFT ANTI JOIN → flag for NOT EXISTS rewrite",
             r"\bLEFT\s+ANTI\s+JOIN\b",
             "/* TODO: Rewrite LEFT ANTI JOIN to WHERE NOT EXISTS (...) or LEFT JOIN ... WHERE key IS NULL */ LEFT JOIN")

        # ── Date format specifier conversion (Java→MySQL style) ───────
        _add(r, "date_format/date_parse format: Java→MySQL specifiers",
             r"\b(DATE_FORMAT|DATE_PARSE)\s*\(\s*(" + _ARG + r")\s*,\s*'([^']+)'\s*\)",
             lambda m: (f"{m.group(1)}({m.group(2).strip()}, "
                        f"'{_convert_java_to_mysql_format(m.group(3))}')"))

        # ── System / session functions ──────────────────────────────────
        # Presto uses bare keywords (no parens) for these
        _add(r, "logged_in_user() → current_user",
             r"\bLOGGED_IN_USER\s*\(\s*\)", "current_user")
        _add(r, "current_database() → current_schema",
             r"\bCURRENT_DATABASE\s*\(\s*\)", "current_schema")

        # Identifiers / misc
        _add(r, "Backtick identifiers → double-quotes",
             r"`([^`]+)`", r'"\1"')
        _add(r, "SHOW DATABASES → SHOW SCHEMAS",
             r"\bSHOW\s+DATABASES\b", "SHOW SCHEMAS")

        return r


# ===========================================================================
#  CONVERTER REGISTRY
# ===========================================================================

_CONVERTERS = {
    "impala": ImpalaToTrinoConverter,
    "hive":   HiveToPrestoConverter,
}


# ===========================================================================
#  NASHORN JS STRING SCANNER  (unchanged from v1)
# ===========================================================================

_JS_TOKEN_RE = re.compile(
    r'(?P<linecomment>//[^\n]*)'
    r'|(?P<blockcomment>/\*[\s\S]*?\*/)'
    r'|(?P<dqstr>"(?:[^"\\]|\\.)*")'
    r"|(?P<sqstr>'(?:[^'\\]|\\.)*')"
)

_SQL_KEYWORD_RE = re.compile(
    r'^\s*(?:--|/\*)?\s*'
    r'(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|MERGE|'
    r'WITH|SHOW|DESCRIBE|EXPLAIN|COMPUTE|INVALIDATE|REFRESH|USE|SET|'
    r'GRANT|REVOKE|UPSERT|MSCK|ANALYZE)\b',
    re.IGNORECASE
)

_SQL_CONTEXT_RE = re.compile(
    r'(?:sql|query|stmt|ddl|dml|hql|statement)\s*(?:[+]?=|\()',
    re.IGNORECASE
)

_JDBC_RE = re.compile(
    r'\.(?:execute(?:Query|Update|Batch)?|prepareStatement|prepareCall)\s*\(',
    re.IGNORECASE
)


def _unescape_js(s: str, quote: str) -> str:
    s = s.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r')
    s = s.replace(f'\\{quote}', quote).replace('\\\\', '\\')
    return s


def _escape_js(s: str, quote: str) -> str:
    s = s.replace('\\', '\\\\')
    s = s.replace(quote, f'\\{quote}')
    s = s.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    return s


def _is_sql(content: str, source: str, start: int) -> bool:
    stripped = content.strip()
    if len(stripped) < 6:
        return False
    if _SQL_KEYWORD_RE.match(stripped):
        return True
    ctx = source[max(0, start - 150):start]
    if _SQL_CONTEXT_RE.search(ctx):
        sql_kw = re.search(
            r'\b(SELECT|FROM|WHERE|INSERT|CREATE|DROP|JOIN|GROUP\s+BY|ORDER\s+BY|'
            r'HAVING|UNION|VALUES|INTO|SET|TABLE|ALTER|PARTITION|OVER|LATERAL)\b',
            stripped, re.IGNORECASE)
        if sql_kw:
            return True
        if len(stripped) > 15:
            return True
    if _JDBC_RE.search(ctx):
        return True
    return False


def find_sql_strings(source):
    # type: (str) -> List[StringLiteral]
    all_strings = []  # List[StringLiteral]
    for m in _JS_TOKEN_RE.finditer(source):
        dq = m.group('dqstr')
        sq = m.group('sqstr')
        if dq or sq:
            raw_full = dq or sq
            quote = raw_full[0]
            inner_raw = raw_full[1:-1]
            inner = _unescape_js(inner_raw, quote)
            lit = StringLiteral(
                start=m.start(), end=m.end(),
                quote=quote, raw=inner_raw,
                unescaped=inner)
            lit.is_sql = _is_sql(inner, source, m.start())
            all_strings.append(lit)

    if not all_strings:
        return []

    # Group strings connected by '+' and propagate SQL detection
    groups = []  # List[List[int]]
    current_group = [0]
    for i in range(1, len(all_strings)):
        prev = all_strings[i - 1]
        curr = all_strings[i]
        between = source[prev.end:curr.start].strip()
        if between == '+' or (between.startswith('+') and
                              not between.startswith('+=')):
            current_group.append(i)
        else:
            groups.append(current_group)
            current_group = [i]
    groups.append(current_group)

    for group_indices in groups:
        if any(all_strings[i].is_sql for i in group_indices):
            for i in group_indices:
                all_strings[i].is_sql = True

    return [s for s in all_strings if s.is_sql]


# ===========================================================================
#  FILE CONVERTER  (now dialect-aware)
# ===========================================================================

class FileConverter:

    # File extensions treated as raw SQL (rules applied to entire content).
    # Everything else (e.g. .js) is treated as a script with embedded SQL
    # inside string literals.
    RAW_SQL_EXTENSIONS = {'.sql', '.hql', '.hive', '.ddl', '.dml'}

    def __init__(self, annotate=False):
        self.annotate = annotate

    def convert_file(self, source, filepath="<stdin>"):
        # type: (str, str) -> Tuple[str, ConversionReport]
        report = ConversionReport(file=filepath)

        # 1. Detect dialect directive
        directive_match = detect_dialect(source)
        if not directive_match:
            log.warning(f"{filepath}: no --!<dialect> directive found – skipping")
            report.skipped = True
            report.dialect = "unknown"
            return source, report

        dialect = directive_match.group(1).lower()
        report.dialect = dialect

        # 2. Check if conversion is needed
        if dialect == "null":
            log.info(f"{filepath}: --!null directive – skipping (no conversion)")
            report.skipped = True
            return source, report

        if dialect in ("trino", "presto"):
            log.info(f"{filepath}: already marked --!{dialect} – skipping")
            report.skipped = True
            return source, report

        if dialect not in DIALECT_MAP:
            log.warning(f"{filepath}: unknown dialect --!{dialect} – skipping")
            report.skipped = True
            return source, report

        target, new_directive = DIALECT_MAP[dialect]
        report.target = target

        # 3. Build the appropriate converter
        converter_cls = _CONVERTERS[dialect]
        converter = converter_cls(annotate=self.annotate)

        # 4. Choose conversion strategy based on file type
        ext = Path(filepath).suffix.lower()
        if ext in self.RAW_SQL_EXTENSIONS:
            result = self._convert_raw_sql(source, converter, report)
        else:
            result = self._convert_embedded_sql(source, converter, report)

        # 5. Replace the dialect directive
        old_directive = directive_match.group(0)   # e.g. "--!impala"
        result = result.replace(old_directive, new_directive, 1)

        return result, report

    @staticmethod
    def _convert_raw_sql(source: str, converter: SQLConverter,
                         report: ConversionReport) -> str:
        """
        For .sql / .hql / .ddl files: apply conversion rules to the SQL
        portions of the file while protecting scripting constructs:

          - --!javascript ... --!endjavascript  blocks (entire block)
          - --!eval  lines (JavaScript expressions)
          - --!if / --!else / --!endif           directives
          - --!forquery / --!do / --!done        directives
          - Any other --! directive lines
        """
        # Split file into PROTECTED (scripting) and CONVERTIBLE (SQL) segments.
        # A segment is a tuple (text, protected:bool).
        segments = []  # List[Tuple[str, bool]]
        pos = 0
        in_js_block = False
        js_block_start = -1

        lines = source.split('\n')
        line_starts = []
        offset = 0
        for line in lines:
            line_starts.append(offset)
            offset += len(line) + 1  # +1 for the newline

        i = 0
        # Track the start of the current SQL region
        sql_region_start = 0

        while i < len(lines):
            line = lines[i]
            stripped = line.strip().lower()

            if not in_js_block and stripped.startswith('--!javascript'):
                # Flush any SQL before this block
                if line_starts[i] > sql_region_start:
                    segments.append((source[sql_region_start:line_starts[i]], False))
                # Enter JS block – find matching --!endjavascript
                js_start = line_starts[i]
                j = i + 1
                while j < len(lines):
                    if lines[j].strip().lower().startswith('--!endjavascript'):
                        break
                    j += 1
                # Include the --!endjavascript line
                js_end = line_starts[j] + len(lines[j]) + 1 if j < len(lines) else len(source)
                segments.append((source[js_start:js_end], True))
                sql_region_start = js_end
                i = j + 1
                continue

            elif stripped.startswith('--!eval'):
                # Protect the entire --!eval line
                if line_starts[i] > sql_region_start:
                    segments.append((source[sql_region_start:line_starts[i]], False))
                line_end = line_starts[i] + len(line) + 1
                segments.append((source[line_starts[i]:line_end], True))
                sql_region_start = line_end
                i += 1
                continue

            elif stripped.startswith('--!') and not stripped.startswith('--!impala') \
                    and not stripped.startswith('--!hive') \
                    and not stripped.startswith('--!null') \
                    and not stripped.startswith('--!trino') \
                    and not stripped.startswith('--!presto'):
                # Protect other directive lines (--!if, --!else, --!endif,
                # --!forquery, --!do, --!done, --!macro, etc.)
                if line_starts[i] > sql_region_start:
                    segments.append((source[sql_region_start:line_starts[i]], False))
                line_end = line_starts[i] + len(line) + 1
                segments.append((source[line_starts[i]:line_end], True))
                sql_region_start = line_end
                i += 1
                continue

            i += 1

        # Flush remaining SQL
        if sql_region_start < len(source):
            segments.append((source[sql_region_start:], False))

        # Convert only the unprotected (SQL) segments
        result_parts = []
        sql_segment_count = 0
        segments_changed = 0
        for text, protected in segments:
            if protected:
                result_parts.append(text)
            else:
                sql_segment_count += 1
                converted, rules = converter.convert(text)
                if rules:
                    segments_changed += 1
                    report.rules_applied.extend(rules)
                result_parts.append(converted)

        report.strings_found = sql_segment_count
        report.strings_changed = segments_changed

        return ''.join(result_parts)

    @staticmethod
    def _convert_embedded_sql(source: str, converter: SQLConverter,
                              report: ConversionReport) -> str:
        """
        For .js (Nashorn) files: scan for SQL inside JS string literals,
        convert each one independently, and rebuild the source.
        """
        sql_strings = find_sql_strings(source)
        report.strings_found = len(sql_strings)

        for lit in sql_strings:
            converted, rules = converter.convert(lit.unescaped)
            if rules:
                lit.converted = converted
                report.strings_changed += 1
                report.rules_applied.extend(rules)

        # Rebuild – replace in reverse offset order so positions stay valid
        result = source
        for lit in sorted(sql_strings, key=lambda l: l.start, reverse=True):
            if lit.converted is None or lit.converted == lit.unescaped:
                continue
            escaped = _escape_js(lit.converted, lit.quote)
            replacement = f'{lit.quote}{escaped}{lit.quote}'
            result = result[:lit.start] + replacement + result[lit.end:]

        return result


# ===========================================================================
#  FLAG SCANNER — finds items needing manual review in converted output
# ===========================================================================

# Patterns to scan for in converted output, with category tags.
# Each tuple is (compiled_regex, category_string).
_FLAG_PATTERNS = [
    (re.compile(r'/\*\s*TODO:\s*(.+?)\*/', re.IGNORECASE),
     "TODO"),
    (re.compile(r'/\*\s*REMOVED:\s*(.+?)\*/', re.IGNORECASE),
     "REMOVED"),
    (re.compile(r'/\*\s*Was INSERT OVERWRITE\b(.+?)\*/', re.IGNORECASE),
     "INSERT OVERWRITE"),
    (re.compile(r'/\*.*?adjust for (Trino|Presto) connector.*?\*/', re.IGNORECASE),
     "DDL ADJUSTMENT"),
]


def scan_for_flags(converted_text, filepath):
    # type: (str, str) -> List
    """
    Scan converted output for markers that need manual review.
    Returns a list of (line_number, category, message) tuples.
    """
    flags = []
    for line_num, line in enumerate(converted_text.split('\n'), 1):
        for pattern, category in _FLAG_PATTERNS:
            for m in pattern.finditer(line):
                # Extract the comment text, cleaned up
                comment = m.group(0).strip()
                # Trim leading /* and trailing */
                inner = comment
                if inner.startswith('/*'):
                    inner = inner[2:]
                if inner.endswith('*/'):
                    inner = inner[:-2]
                inner = inner.strip()
                flags.append((line_num, category, inner))
    return flags


def generate_conversion_report(reports, report_path):
    # type: (List[ConversionReport], str) -> None
    """
    Write a conversion report listing all files with items that need
    manual attention (TODOs, removed statements, DDL adjustments)
    and any detected user-defined functions.
    """
    flagged_reports = [r for r in reports if r.flags]
    udf_reports = [r for r in reports if r.udfs]

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("=" * 78 + "\n")
        f.write("  SQL DIALECT CONVERSION REPORT\n")
        f.write("=" * 78 + "\n\n")

        # Summary
        total = len(reports)
        skipped = sum(1 for r in reports if r.skipped)
        converted = total - skipped
        flagged = len(flagged_reports)
        total_flags = sum(len(r.flags) for r in flagged_reports)

        # UDF summary
        all_udf_names = set()
        total_udf_calls = 0
        for r in udf_reports:
            for _, name in r.udfs:
                all_udf_names.add(name.upper())
                total_udf_calls += 1
        udf_count = len(all_udf_names)

        f.write(f"Files processed:        {total}\n")
        f.write(f"Files converted:        {converted}\n")
        f.write(f"Files skipped:          {skipped}\n")
        f.write(f"Files with flags:       {flagged}\n")
        f.write(f"Total flags:            {total_flags}\n")
        f.write(f"Files with UDFs:        {len(udf_reports)}\n")
        f.write(f"Unique UDFs detected:   {udf_count}\n")
        f.write(f"Total UDF references:   {total_udf_calls}\n")

        # ── Breakdown by dialect pair ──────────────────────────────
        dialect_groups = {}  # (dialect, target) -> list of reports
        for r in reports:
            if r.skipped:
                continue
            key = (r.dialect, r.target)
            if key not in dialect_groups:
                dialect_groups[key] = []
            dialect_groups[key].append(r)

        if dialect_groups:
            f.write("\nConversions by dialect:\n")
            for (dialect, target), group in sorted(dialect_groups.items()):
                g_flags = sum(len(r.flags) for r in group)
                g_udf_files = sum(1 for r in group if r.udfs)
                f.write(f"  --!{dialect} → --!{target}:  "
                        f"{len(group)} file(s), "
                        f"{g_flags} flag(s), "
                        f"{g_udf_files} file(s) with UDFs\n")

        # ── Flag details ───────────────────────────────────────────
        if flagged_reports:
            # Summary by category
            category_counts = {}
            for r in flagged_reports:
                for _, category, _ in r.flags:
                    category_counts[category] = category_counts.get(category, 0) + 1

            f.write("\nFlags by category:\n")
            for category in sorted(category_counts.keys()):
                f.write(f"  {category}: {category_counts[category]}\n")

            # Per-file detail
            f.write("\n" + "-" * 78 + "\n")
            f.write("  DETAILED FLAGS BY FILE\n")
            f.write("-" * 78 + "\n")

            for r in flagged_reports:
                f.write(f"\n{r.file}")
                if r.dialect and r.target:
                    f.write(f"  [--!{r.dialect} → --!{r.target}]")
                f.write("\n")

                for line_num, category, message in r.flags:
                    f.write(f"  Line {line_num:>5}  [{category}]  {message}\n")
        else:
            f.write("\nNo conversion flags require manual review.\n")

        # ── UDF details ────────────────────────────────────────────
        f.write("\n" + "-" * 78 + "\n")
        f.write("  USER-DEFINED FUNCTIONS DETECTED\n")
        f.write("-" * 78 + "\n")

        if not udf_reports:
            f.write("\nNo user-defined functions detected.\n")
        else:
            # Cross-file UDF summary: each unique UDF with all locations
            udf_index = {}  # name_upper -> [(file, line_num, original_name)]
            for r in udf_reports:
                for line_num, name in r.udfs:
                    upper = name.upper()
                    if upper not in udf_index:
                        udf_index[upper] = []
                    udf_index[upper].append((r.file, line_num, name))

            f.write(f"\n{udf_count} unique function(s) not recognized as "
                    f"built-in, found in {len(udf_reports)} file(s).\n")
            f.write("These may be user-defined functions that require "
                    "manual porting to Trino/Presto.\n")

            for udf_name in sorted(udf_index.keys()):
                locations = udf_index[udf_name]
                original = locations[0][2]  # preserve original casing
                f.write(f"\n  {original}()  "
                        f"({len(locations)} occurrence(s))\n")
                for fpath, line_num, _ in locations:
                    f.write(f"    {fpath}  line {line_num}\n")

        f.write("\n" + "=" * 78 + "\n")

    log.info(f"Conversion report written to {report_path}"
             f" ({flagged} flagged file(s), {total_flags} flag(s), "
             f"{udf_count} UDF(s) detected)")


# ===========================================================================
#  UDF DETECTOR — identifies likely user-defined functions
# ===========================================================================

# SQL keywords and clauses that look like function calls but aren't.
_SQL_KEYWORDS = frozenset(k.upper() for k in [
    'select', 'from', 'where', 'and', 'or', 'not', 'in', 'on', 'as',
    'case', 'when', 'then', 'else', 'end', 'is', 'null', 'between',
    'like', 'exists', 'having', 'group', 'order', 'by', 'asc', 'desc',
    'limit', 'offset', 'union', 'all', 'distinct', 'into', 'values',
    'set', 'join', 'left', 'right', 'inner', 'outer', 'cross', 'full',
    'insert', 'update', 'delete', 'create', 'drop', 'alter', 'table',
    'view', 'index', 'database', 'schema', 'if', 'over', 'partition',
    'rows', 'range', 'unbounded', 'preceding', 'following', 'current',
    'row', 'with', 'recursive', 'temporary', 'temp', 'external',
    'interval', 'true', 'false', 'primary', 'key', 'foreign',
    'references', 'constraint', 'default', 'check', 'unique',
    'grant', 'revoke', 'to', 'role', 'use', 'show', 'describe',
    'explain', 'analyze', 'truncate', 'merge', 'using', 'matched',
    'filter', 'within', 'array', 'map', 'struct', 'lateral',
])

# Known built-in function names across Impala, Hive, Trino, and Presto.
# Any function call NOT in this set is flagged as a potential UDF.
_KNOWN_BUILTINS = frozenset(k.upper() for k in [
    # ── Aggregate ──────────────────────────────────────────────────
    'avg', 'count', 'max', 'min', 'sum',
    'group_concat', 'array_agg', 'array_join',
    'collect_list', 'collect_set', 'set_agg',
    'ndv', 'approx_distinct', 'approx_count_distinct',
    'appx_median', 'approx_percentile', 'percentile_approx',
    'percentile', 'percentile_cont', 'percentile_disc',
    'stddev', 'stddev_pop', 'stddev_samp',
    'variance', 'variance_pop', 'variance_samp', 'var_pop', 'var_samp',
    'corr', 'covar_pop', 'covar_samp',
    'regr_avgx', 'regr_avgy', 'regr_count', 'regr_intercept',
    'regr_r2', 'regr_slope', 'regr_sxx', 'regr_sxy', 'regr_syy',
    'histogram', 'numeric_histogram',
    'bool_and', 'bool_or', 'every', 'any_value',
    'count_if', 'sum_distinct', 'count_distinct',
    'listagg', 'string_agg',
    'bit_and', 'bit_or', 'bit_xor',
    'checksum', 'arbitrary',
    'max_by', 'min_by',
    'map_agg', 'map_union', 'multimap_agg',
    'reduce_agg', 'merge',
    # ── Analytic / window ──────────────────────────────────────────
    'row_number', 'rank', 'dense_rank', 'percent_rank', 'cume_dist',
    'ntile', 'lag', 'lead',
    'first_value', 'last_value', 'nth_value',
    # ── Math ───────────────────────────────────────────────────────
    'abs', 'ceil', 'ceiling', 'floor', 'round', 'truncate', 'trunc',
    'mod', 'pmod', 'power', 'pow', 'sqrt', 'cbrt', 'exp',
    'ln', 'log', 'log2', 'log10',
    'sign', 'positive', 'negative',
    'sin', 'cos', 'tan', 'asin', 'acos', 'atan', 'atan2',
    'sinh', 'cosh', 'tanh',
    'degrees', 'radians', 'pi', 'e',
    'rand', 'random',
    'greatest', 'least',
    'width_bucket',
    'fnv_hash', 'xxhash64', 'murmur_hash', 'md5', 'sha1', 'sha2',
    'sha256', 'sha512', 'crc32',
    'bin', 'hex', 'unhex',
    'conv', 'shiftleft', 'shiftright', 'shiftrightunsigned',
    'bitand', 'bitor', 'bitxor', 'bitnot',
    'factorial', 'is_nan', 'is_inf',
    'infinity', 'nan', 'from_base', 'to_base',
    'bit_count', 'cosine_similarity',
    # ── String ─────────────────────────────────────────────────────
    'ascii', 'chr', 'char', 'char_length', 'character_length',
    'length', 'octet_length', 'bit_length',
    'concat', 'concat_ws',
    'lower', 'lcase', 'upper', 'ucase', 'initcap',
    'lpad', 'rpad', 'ltrim', 'rtrim', 'trim', 'btrim',
    'substr', 'substring', 'strleft', 'strright',
    'left', 'right', 'mid',
    'instr', 'strpos', 'locate', 'position', 'charindex',
    'find_in_set',
    'replace', 'translate', 'overlay',
    'repeat', 'reverse', 'space',
    'split', 'split_part', 'split_to_map', 'split_to_multimap',
    'starts_with', 'ends_with',
    'regexp_extract', 'regexp_replace', 'regexp_like',
    'regexp_count', 'regexp_split', 'regexp_extract_all',
    'format', 'printf', 'format_number',
    'soundex', 'levenshtein', 'levenshtein_distance', 'hamming_distance',
    'base64', 'unbase64', 'to_utf8', 'from_utf8',
    'encode', 'decode',
    'url_encode', 'url_decode', 'url_extract_host',
    'url_extract_path', 'url_extract_port', 'url_extract_protocol',
    'url_extract_query', 'url_extract_parameter', 'url_extract_fragment',
    'json_extract', 'json_extract_scalar', 'get_json_object',
    'json_format', 'json_parse', 'json_array_get',
    'json_array_length', 'json_size',
    'parse_url', 'word_stem', 'normalize',
    'codepoint', 'luhn_check',
    # ── Date / time ────────────────────────────────────────────────
    'now', 'current_timestamp', 'current_date', 'current_time',
    'localtimestamp', 'localtime',
    'date', 'time', 'timestamp',
    'year', 'quarter', 'month', 'week', 'week_of_year', 'yearweek',
    'day', 'dayofmonth', 'dayofweek', 'dayofyear', 'dayname', 'monthname',
    'day_of_month', 'day_of_week', 'day_of_year',
    'hour', 'minute', 'second', 'millisecond',
    'date_format', 'date_parse', 'format_datetime', 'parse_datetime',
    'from_timestamp', 'to_timestamp',
    'date_add', 'date_sub', 'date_diff', 'datediff',
    'date_trunc', 'date_part', 'extract',
    'add_months', 'months_between', 'last_day', 'next_day',
    'months_add', 'months_sub',
    'days_add', 'days_sub',
    'hours_add', 'hours_sub',
    'minutes_add', 'minutes_sub',
    'seconds_add', 'seconds_sub',
    'weeks_add', 'weeks_sub',
    'years_add', 'years_sub',
    'unix_timestamp', 'to_unixtime', 'from_unixtime',
    'from_utc_timestamp', 'to_utc_timestamp',
    'to_date',
    'from_iso8601_date', 'from_iso8601_timestamp',
    'to_iso8601', 'to_milliseconds',
    'at_timezone', 'with_timezone',
    'human_readable_seconds',
    'sequence',
    # ── Type conversion / conditional ──────────────────────────────
    'cast', 'try_cast', 'typeof', 'coalesce',
    'if', 'iff', 'ifnull', 'isnull', 'nullif', 'nullifzero', 'zeroifnull',
    'nvl', 'nvl2', 'decode',
    'try', 'try_cast',
    # ── Collection / complex type ──────────────────────────────────
    'size', 'cardinality',
    'array', 'array_contains', 'contains',
    'array_sort', 'sort_array', 'array_distinct', 'array_union',
    'array_except', 'array_intersect', 'array_join',
    'array_max', 'array_min', 'array_position', 'array_remove',
    'element_at', 'slice', 'flatten', 'zip', 'zip_with',
    'transform', 'filter', 'reduce', 'any_match', 'all_match',
    'map', 'map_keys', 'map_values', 'map_entries',
    'map_from_entries', 'map_filter', 'transform_keys', 'transform_values',
    'map_concat', 'map_zip_with',
    'named_struct', 'struct',
    'explode', 'posexplode', 'inline',
    'unnest',
    # ── System / session / misc ────────────────────────────────────
    'user', 'current_user', 'session_user', 'system_user',
    'effective_user', 'logged_in_user',
    'current_database', 'current_schema', 'current_catalog',
    'current_groups', 'current_role', 'current_path',
    'current_timezone',
    'version', 'pid', 'coordinator', 'sleep',
    'uuid',
    'reflect', 'java_method',
    'assert_true',
    'raise_error',
    'typeof',
    'input_file_name', 'input_file_block_start', 'input_file_block_length',
    # ── Trino/Presto-specific that might appear in converted output ─
    'approx_distinct', 'approx_set', 'approx_most_frequent',
    'tdigest_agg', 'value_at_quantile',
    'qdigest_agg',
    'from_base64', 'to_base64', 'from_hex', 'to_hex',
    'hmac_md5', 'hmac_sha1', 'hmac_sha256', 'hmac_sha512',
    'spooky_hash_v2_32', 'spooky_hash_v2_64',
    'render', 'bar', 'color',
    'ip_prefix', 'ip_subnet_min', 'ip_subnet_max', 'ip_subnet_range',
    'is_subnet_of', 'is_private_ip',
    'features',
    'classification_miss_rate', 'classification_fall_out',
    'classification_precision', 'classification_recall',
    'classification_thresholds',
    'wilson_interval_lower', 'wilson_interval_upper',
])

# Regex to find function-call patterns: word followed by '('
_FUNC_CALL_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\(')

# Patterns for schema-qualified function calls: schema.func(
_QUALIFIED_FUNC_RE = re.compile(
    r'\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*\(')


def _strip_sql_comments(source):
    # type: (str) -> str
    """
    Remove SQL comments from source while preserving line count
    (so line numbers remain accurate for reporting).
    Handles:
      - Line comments: everything after '--' (that isn't inside a string)
      - Block comments: /* ... */ including multi-line
    """
    result = []
    in_block_comment = False
    for line in source.split('\n'):
        out = []
        i = 0
        in_string = None  # tracks ' or " when inside a string literal
        while i < len(line):
            ch = line[i]

            if in_block_comment:
                if ch == '*' and i + 1 < len(line) and line[i + 1] == '/':
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue

            if in_string:
                out.append(ch)
                if ch == in_string:
                    in_string = None
                elif ch == '\\':
                    # skip escaped char
                    i += 1
                    if i < len(line):
                        out.append(line[i])
                i += 1
                continue

            # Not in comment, not in string
            if ch in ("'", '"'):
                in_string = ch
                out.append(ch)
                i += 1
            elif ch == '-' and i + 1 < len(line) and line[i + 1] == '-':
                # Line comment — skip rest of line
                break
            elif ch == '/' and i + 1 < len(line) and line[i + 1] == '*':
                in_block_comment = True
                i += 2
            else:
                out.append(ch)
                i += 1

        result.append(''.join(out))
    return '\n'.join(result)


def scan_for_udfs(source, filepath):
    # type: (str, str) -> List
    """
    Scan SQL source for function calls that are not known built-ins.
    Returns a list of (line_number, function_name) tuples.

    Works on the ORIGINAL source (before conversion) so it captures
    the functions as they were written, not after rewriting.
    """
    udfs = []
    seen = set()  # (func_name_upper, line_num) to deduplicate

    # Strip SQL comments (preserves line numbers)
    cleaned = _strip_sql_comments(source)

    # Split both original (for directive detection) and cleaned (for scanning)
    orig_lines = source.split('\n')
    clean_lines = cleaned.split('\n')
    in_js_block = False

    for line_num, (orig, line) in enumerate(zip(orig_lines, clean_lines), 1):
        orig_stripped = orig.strip().lower()

        # Skip --!javascript blocks entirely (check original)
        if orig_stripped.startswith('--!javascript'):
            in_js_block = True
            continue
        if orig_stripped.startswith('--!endjavascript'):
            in_js_block = False
            continue
        if in_js_block:
            continue

        # Skip --!eval lines and other directives (check original)
        if orig_stripped.startswith('--!'):
            continue

        # Skip lines that are entirely empty after comment stripping
        if not line.strip():
            continue

        # Find schema-qualified function calls (almost always UDFs)
        for m in _QUALIFIED_FUNC_RE.finditer(line):
            func_name = m.group(1)
            key = (func_name.upper(), line_num)
            if key not in seen:
                seen.add(key)
                udfs.append((line_num, func_name))

        # Find unqualified function calls
        for m in _FUNC_CALL_RE.finditer(line):
            func_name = m.group(1)
            upper = func_name.upper()

            # Skip if it's a known built-in or SQL keyword
            if upper in _KNOWN_BUILTINS or upper in _SQL_KEYWORDS:
                continue

            # Skip if it's part of a qualified name we already caught
            # (check if there's a dot before the function name)
            start = m.start(1)
            if start > 0 and line[start - 1] == '.':
                continue

            key = (upper, line_num)
            if key not in seen:
                seen.add(key)
                udfs.append((line_num, func_name))

    return udfs


# ===========================================================================
#  CLI
# ===========================================================================

def process_file(input_path, output_path=None, dry_run=False, annotate=False):
    # type: (str, Optional[str], bool, bool) -> ConversionReport
    converter = FileConverter(annotate=annotate)
    with open(input_path, 'r', encoding='utf-8') as f:
        source = f.read()

    result, report = converter.convert_file(source, filepath=input_path)

    # Scan converted output for items needing manual review,
    # and scan original source for user-defined functions.
    if not report.skipped:
        out_file = output_path or input_path
        report.flags = scan_for_flags(result, out_file)
        report.udfs = scan_for_udfs(source, input_path)

    if report.skipped:
        if not dry_run and output_path:
            # Still copy the file so output directory is complete
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result)
    elif dry_run:
        rules_count = len(set(report.rules_applied))
        log.info(f"[DRY RUN] {report.file}: --!{report.dialect} → --!{report.target}  "
                 f"{rules_count} rules applied across "
                 f"{report.strings_changed}/{report.strings_found} SQL regions")
        for rule in sorted(set(report.rules_applied)):
            log.info(f"  → {rule}")
    else:
        out = output_path or input_path
        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            f.write(result)
        rules_count = len(set(report.rules_applied))
        log.info(f"Converted {report.file} → {out}  "
                 f"[--!{report.dialect} → --!{report.target}]  "
                 f"({rules_count} rules applied, "
                 f"{report.strings_changed}/{report.strings_found} SQL regions changed)")

    return report


def process_directory(input_dir, output_dir,
                      recursive, dry_run, annotate=False,
                      converted_only=False):
    # type: (str, str, bool, bool, bool, bool) -> List[ConversionReport]
    """
    Process all files in input_dir.  Supported SQL/JS files are converted;
    all other files are copied unchanged to output_dir (unless
    converted_only=True, which skips unsupported file types).
    """
    reports = []
    supported_extensions = {'.js', '.sql', '.hql', '.hive', '.ddl', '.dml'}
    copied_count = 0

    if recursive:
        all_files = sorted(Path(input_dir).rglob('*'))
    else:
        all_files = sorted(p for p in Path(input_dir).iterdir() if p.is_file())

    for fpath in all_files:
        if not fpath.is_file():
            continue
        rel = fpath.relative_to(input_dir)
        out = str(Path(output_dir) / rel)

        if fpath.suffix.lower() in supported_extensions:
            # Supported file — run the converter
            reports.append(process_file(str(fpath), out, dry_run, annotate))
        elif not converted_only and not dry_run:
            # Unsupported file — copy as-is
            os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
            shutil.copy2(str(fpath), out)
            copied_count += 1

    if copied_count > 0:
        log.info(f"Copied {copied_count} non-SQL file(s) to {output_dir}")

    return reports


def main():
    parser = argparse.ArgumentParser(
        description="Convert SQL dialects in .js and .sql files based on "
                    "--!impala / --!hive / --!null dialect directives."
    )
    parser.add_argument("input", nargs='?', help="Input .js/.sql file or directory")
    parser.add_argument("-o", "--output", help="Output file or directory")
    parser.add_argument("-r", "--recursive", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Preview changes without writing")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--annotate", action="store_true",
                        help="Add comments showing removed/replaced code "
                             "(default: clean output without comments)")
    parser.add_argument("--converted-only", action="store_true",
                        help="In directory mode, only output converted SQL/JS "
                             "files (default: copy all files to output)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in self-test")
    args = parser.parse_args()

    if args.self_test or not args.input:
        _self_test()
        return

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    inp = args.input
    reports = []

    if os.path.isdir(inp):
        if not args.output:
            log.error("Output directory (-o) required for directory input.")
            sys.exit(1)
        reports = process_directory(inp, args.output, args.recursive,
                                    args.dry_run, args.annotate,
                                    args.converted_only)
        converted = [r for r in reports if not r.skipped]
        skipped   = [r for r in reports if r.skipped]
        log.info(f"\nProcessed {len(reports)} files: "
                 f"{len(converted)} converted, {len(skipped)} skipped.")
    elif os.path.isfile(inp):
        report = process_file(inp, args.output, args.dry_run, args.annotate)
        reports = [report]
    else:
        log.error(f"Not found: {inp}")
        sys.exit(1)

    # Generate conversion report in the output directory
    if reports and not args.dry_run:
        if args.output:
            if os.path.isdir(inp):
                report_dir = args.output
            else:
                report_dir = os.path.dirname(args.output) or '.'
        else:
            report_dir = os.path.dirname(os.path.abspath(inp))
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, "conversion_report.txt")
        generate_conversion_report(reports, report_path)


# ===========================================================================
#  SELF-TEST  –  three files: --!impala, --!hive, --!null
# ===========================================================================

SAMPLE_IMPALA = r'''
// --!impala
// Nashorn JS – Impala ETL
var JavaImporter = new JavaImporter(java.sql);

function getConnection() {
    var driver = "org.apache.hive.jdbc.HiveDriver";
    java.lang.Class.forName(driver);
    return java.sql.DriverManager.getConnection(
        "jdbc:hive2://impala-host:21050/default;auth=noSasl"
    );
}

function runETL(conn, dateParam) {
    var refreshSql = "INVALIDATE METADATA db.source_table";
    conn.createStatement().execute(refreshSql);

    var dropSql = "DROP TABLE IF EXISTS db.target_table";
    conn.createStatement().execute(dropSql);

    var createSql = "CREATE TABLE db.target_table (" +
                    "  id BIGINT," +
                    "  name STRING," +
                    "  score FLOAT," +
                    "  created_date TIMESTAMP" +
                    ") STORED AS PARQUET";
    conn.createStatement().execute(createSql);

    var insertSql = "INSERT OVERWRITE TABLE db.target_table " +
                    "SELECT id, " +
                    "  NVL(name, 'unknown'), " +
                    "  CAST(score AS STRING), " +
                    "  TO_DATE(created_ts) " +
                    "FROM db.source_table " +
                    "WHERE DATEDIFF(NOW(), created_ts) <= 30";
    conn.createStatement().execute(insertSql);

    var statsSql = "COMPUTE STATS db.target_table";
    conn.createStatement().execute(statsSql);

    var querySql = "SELECT `region`, GROUP_CONCAT(`status`), " +
                   "  UNIX_TIMESTAMP(), " +
                   "  DATE_ADD(hire_date, 30), " +
                   "  MONTHS_BETWEEN(end_date, start_date) " +
                   "FROM db.employees " +
                   "WHERE FNV_HASH(employee_id) % 10 = 0";
    var rs = conn.createStatement().executeQuery(querySql);

    while (rs.next()) {
        print("Region: " + rs.getString(1));
    }

    // Pure JavaScript (must NOT change)
    var results = [];
    for (var i = 0; i < 10; i++) {
        results.push("item_" + i);
    }
    var msg = "Processing complete for date: " + dateParam;
    print(msg);
    var config = { "batchSize": 1000, "retryCount": 3 };
    print("Config: " + JSON.stringify(config));
    return results;
}

var conn = getConnection();
try { runETL(conn, "2024-01-15"); } finally { conn.close(); }
'''

SAMPLE_HIVE = r'''
// --!hive
// Nashorn JS – Hive ETL
var JavaImporter = new JavaImporter(java.sql);

function runHiveETL(conn) {
    var createSql = "CREATE TABLE db.events (" +
                    "  event_id BIGINT," +
                    "  payload STRING," +
                    "  tags ARRAY<STRING>" +
                    ") STORED AS ORC";
    conn.createStatement().execute(createSql);

    var insertSql = "INSERT OVERWRITE TABLE db.events_summary " +
                    "SELECT event_id, " +
                    "  COLLECT_LIST(payload), " +
                    "  SIZE(tags), " +
                    "  UNIX_TIMESTAMP(), " +
                    "  DATEDIFF(NOW(), created_at) " +
                    "FROM db.events " +
                    "WHERE payload RLIKE '^ERR.*' " +
                    "GROUP BY event_id";
    conn.createStatement().execute(insertSql);

    var explodeSql = "SELECT t.event_id, tag " +
                     "FROM db.events " +
                     "LATERAL VIEW EXPLODE(tags) t_tags AS tag";
    var rs = conn.createStatement().executeQuery(explodeSql);

    var locateSql = "SELECT LOCATE('needle', haystack_col) FROM db.test";
    conn.createStatement().executeQuery(locateSql);

    var pctSql = "SELECT PERCENTILE_APPROX(score, 0.95) FROM db.scores";
    conn.createStatement().executeQuery(pctSql);

    // JavaScript (must NOT change)
    var items = ["alpha", "beta", "gamma"];
    for (var i = 0; i < items.length; i++) {
        print("Item: " + items[i]);
    }
    var total = 42 + 58;
    print("Total = " + total);
}
'''

SAMPLE_NULL = r'''
// --!null
// Nashorn JS – no SQL conversion
function doStuff() {
    var sql = "SELECT * FROM whatever WHERE id = 1";
    print("No conversion should happen here");
    return sql;
}
'''

SAMPLE_IMPALA_SQL = """\
--!impala
-- Raw Impala SQL file
CREATE TABLE db.target (
    id BIGINT,
    name STRING,
    score FLOAT
) STORED AS PARQUET;

INSERT OVERWRITE TABLE db.target
SELECT id,
       NVL(name, 'unknown'),
       CAST(score AS STRING),
       TO_DATE(created_ts),
       TO_DATE(TRUNC(mn.admissiondate, 'MM')),
       GROUP_CONCAT(`status`),
       DATEDIFF(NOW(), TRUNC(ts, 'DD')),
       DECODE(status, 1, 'active', 2, 'inactive', 'unknown'),
       NULLIFZERO(balance),
       ZEROIFNULL(quantity),
       FROM_UTC_TIMESTAMP(event_ts, 'America/New_York')
FROM db.source
LEFT SEMI JOIN db.lookup ON db.source.key = db.lookup.key
WHERE DATEDIFF(NOW(), created_ts) <= 30
  AND name REGEXP '^[A-Z].*';

COMPUTE STATS db.target;
"""

SAMPLE_HIVE_SQL = """\
--!hive
-- Raw Hive SQL file
SELECT
    event_id,
    COLLECT_LIST(payload),
    SIZE(tags),
    UNIX_TIMESTAMP(),
    DATEDIFF(NOW(), created_at),
    DECODE(priority, 'H', 'High', 'M', 'Medium', 'Low'),
    FROM_UTC_TIMESTAMP(event_ts, 'US/Pacific')
FROM db.events
LEFT SEMI JOIN db.valid_events ON db.events.event_id = db.valid_events.event_id
LATERAL VIEW EXPLODE(tags) t_tags AS tag
WHERE payload RLIKE '^ERR.*'
GROUP BY event_id;
"""

SAMPLE_NULL_SQL = """\
--!null
-- This SQL file should not be converted
SELECT NVL(a, 0), CAST(b AS STRING) FROM my_table;
"""


def _self_test():
    converter = FileConverter()
    all_errors = []

    def check(cond, msg):
        if not cond:
            all_errors.append(msg)

    SEP = "=" * 70

    # ─── TEST 1: --!impala .js → --!trino ────────────────────────────
    print(f"\n{SEP}")
    print("  TEST 1: --!impala .js → --!trino")
    print(SEP)

    result, report = converter.convert_file(SAMPLE_IMPALA, "impala_etl.js")
    print(f"  Dialect: {report.dialect} → {report.target}")
    print(f"  SQL strings found: {report.strings_found}, changed: {report.strings_changed}")
    for rule in sorted(set(report.rules_applied)):
        print(f"    ✓ {rule}")

    # Directive
    check('--!trino' in result,                "Directive not changed to --!trino")
    check('--!impala' not in result,           "Old --!impala directive still present")

    # JS untouched
    check('var JavaImporter = new JavaImporter(java.sql);' in result,
          "[impala] JavaImporter modified")
    check('for (var i = 0; i < 10; i++)' in result,
          "[impala] for-loop modified")
    check('results.push("item_"' in result,
          "[impala] results.push modified")
    check('"batchSize"' in result,
          "[impala] config modified")

    # SQL converted
    check('COALESCE(' in result,              "[impala] NVL not converted")
    check('CAST(score AS VARCHAR)' in result, "[impala] CAST AS STRING not converted")
    check('CAST(created_ts AS DATE)' in result, "[impala] TO_DATE not converted")
    check("DATE_DIFF('day'" in result,        "[impala] DATEDIFF not converted")
    check('VARCHAR' in result,                "[impala] STRING not converted")
    check('REAL' in result,                   "[impala] FLOAT not converted")
    check('INSERT INTO' in result,            "[impala] INSERT OVERWRITE not converted")
    check('ARRAY_JOIN(ARRAY_AGG(' in result,  "[impala] GROUP_CONCAT not converted")
    check('TO_UNIXTIME(current_timestamp)' in result,     "[impala] UNIX_TIMESTAMP not converted")
    check('XXHASH64(' in result,              "[impala] FNV_HASH not converted")
    check("DATE_ADD('day'" in result,         "[impala] DATE_ADD not converted")
    check("DATE_DIFF('month'" in result,      "[impala] MONTHS_BETWEEN not converted")
    check('COMPUTE STATS' not in result,
          "[impala] COMPUTE STATS not removed")

    # ─── TEST 2: --!hive .js → --!presto ────────────────────────────
    print(f"\n{SEP}")
    print("  TEST 2: --!hive .js → --!presto")
    print(SEP)

    result2, report2 = converter.convert_file(SAMPLE_HIVE, "hive_etl.js")
    print(f"  Dialect: {report2.dialect} → {report2.target}")
    print(f"  SQL strings found: {report2.strings_found}, changed: {report2.strings_changed}")
    for rule in sorted(set(report2.rules_applied)):
        print(f"    ✓ {rule}")

    # Directive
    check('--!presto' in result2,              "Directive not changed to --!presto")
    check('--!hive' not in result2,            "Old --!hive directive still present")

    # JS untouched
    check('var items = ["alpha", "beta", "gamma"]' in result2,
          "[hive] items array modified")
    check('var total = 42 + 58;' in result2,
          "[hive] total calculation modified")
    check('print("Item: " + items[i])' in result2,
          "[hive] print modified")

    # SQL converted
    check('VARCHAR' in result2,                "[hive] STRING not converted")
    check('INSERT INTO' in result2,            "[hive] INSERT OVERWRITE not converted")
    check('ARRAY_AGG(' in result2,             "[hive] COLLECT_LIST not converted")
    check('CARDINALITY(' in result2,           "[hive] SIZE not converted")
    check('TO_UNIXTIME(current_timestamp)' in result2,     "[hive] UNIX_TIMESTAMP not converted")
    check("DATE_DIFF('day'" in result2,        "[hive] DATEDIFF not converted")
    check('REGEXP_LIKE(' in result2,           "[hive] RLIKE not converted")
    check('CROSS JOIN UNNEST(' in result2,     "[hive] LATERAL VIEW EXPLODE not converted")
    check('STRPOS(haystack_col' in result2,    "[hive] LOCATE not converted")
    check('APPROX_PERCENTILE(' in result2,     "[hive] PERCENTILE_APPROX not converted")
    check('STORED AS ORC' not in result2,
          "[hive] STORED AS not handled")
    check("WITH (format = 'ORC')" in result2,
          "[hive] STORED AS not converted to WITH format")

    # ─── TEST 3: --!null .js → no conversion ─────────────────────────
    print(f"\n{SEP}")
    print("  TEST 3: --!null .js → no conversion")
    print(SEP)

    result3, report3 = converter.convert_file(SAMPLE_NULL, "null_file.js")
    print(f"  Dialect: {report3.dialect}")
    print(f"  Skipped: {report3.skipped}")

    check(report3.skipped is True,            "[null js] file was not skipped")
    check(result3 == SAMPLE_NULL,             "[null js] file content was modified")
    check('--!null' in result3,               "[null js] directive was changed")
    check('SELECT * FROM whatever' in result3,"[null js] SQL was modified")

    # ─── TEST 4: --!impala .sql → --!trino (raw SQL) ─────────────────
    print(f"\n{SEP}")
    print("  TEST 4: --!impala .sql → --!trino (raw SQL file)")
    print(SEP)

    result4, report4 = converter.convert_file(SAMPLE_IMPALA_SQL, "etl_impala.sql")
    print(f"  Dialect: {report4.dialect} → {report4.target}")
    print(f"  Strings changed: {report4.strings_changed}")
    for rule in sorted(set(report4.rules_applied)):
        print(f"    ✓ {rule}")

    check('--!trino' in result4,                "[sql impala] directive not changed to --!trino")
    check('--!impala' not in result4,           "[sql impala] old directive still present")
    check('VARCHAR' in result4,                 "[sql impala] STRING not converted")
    check('REAL' in result4,                    "[sql impala] FLOAT not converted")
    check('COALESCE(' in result4,               "[sql impala] NVL not converted")
    check('CAST(score AS VARCHAR)' in result4,  "[sql impala] CAST AS STRING not converted")
    check('CAST(created_ts AS DATE)' in result4,"[sql impala] TO_DATE not converted")
    # The user's exact failing case: nested TRUNC inside TO_DATE
    check("CAST(DATE_TRUNC('mm', mn.admissiondate) AS DATE)" in result4,
          "[sql impala] TO_DATE(TRUNC(...)) nested parens broken")
    # Nested TRUNC inside DATEDIFF second argument
    check("DATE_DIFF('day', DATE_TRUNC('dd', ts), current_timestamp)" in result4,
          "[sql impala] DATEDIFF(NOW(), TRUNC(...)) nested parens broken")
    check("DATE_DIFF('day'" in result4,         "[sql impala] DATEDIFF not converted")
    check('INSERT INTO' in result4,             "[sql impala] INSERT OVERWRITE not converted")
    check('ARRAY_JOIN(ARRAY_AGG(' in result4,   "[sql impala] GROUP_CONCAT not converted")
    check('COMPUTE STATS' not in result4,
          "[sql impala] COMPUTE STATS not removed")
    check('STORED AS PARQUET' not in result4,
          "[sql impala] STORED AS not handled")
    check("WITH (format = 'PARQUET')" in result4,
          "[sql impala] STORED AS not converted to WITH format")
    # SQL comments must still be there
    check('-- Raw Impala SQL file' in result4,  "[sql impala] SQL comment was mangled")
    # New rules
    check("CASE WHEN status = 1 THEN 'active'" in result4,
          "[sql impala] DECODE not converted to CASE")
    check('NULLIF(balance, 0)' in result4,
          "[sql impala] NULLIFZERO not converted")
    check('COALESCE(quantity, 0)' in result4,
          "[sql impala] ZEROIFNULL not converted")
    check('AT TIME ZONE' in result4,
          "[sql impala] FROM_UTC_TIMESTAMP not converted")
    check('TODO' in result4 and 'SEMI JOIN' in result4,
          "[sql impala] LEFT SEMI JOIN not flagged")
    check("REGEXP_LIKE(name, '^[A-Z].*')" in result4,
          "[sql impala] REGEXP not converted to REGEXP_LIKE")

    # ─── TEST 5: --!hive .sql → --!presto (raw SQL) ──────────────────
    print(f"\n{SEP}")
    print("  TEST 5: --!hive .sql → --!presto (raw SQL file)")
    print(SEP)

    result5, report5 = converter.convert_file(SAMPLE_HIVE_SQL, "etl_hive.sql")
    print(f"  Dialect: {report5.dialect} → {report5.target}")
    print(f"  Strings changed: {report5.strings_changed}")
    for rule in sorted(set(report5.rules_applied)):
        print(f"    ✓ {rule}")

    check('--!presto' in result5,              "[sql hive] directive not changed to --!presto")
    check('--!hive' not in result5,            "[sql hive] old directive still present")
    check('ARRAY_AGG(' in result5,             "[sql hive] COLLECT_LIST not converted")
    check('CARDINALITY(' in result5,           "[sql hive] SIZE not converted")
    check('TO_UNIXTIME(current_timestamp)' in result5,     "[sql hive] UNIX_TIMESTAMP not converted")
    check("DATE_DIFF('day'" in result5,        "[sql hive] DATEDIFF not converted")
    check('REGEXP_LIKE(' in result5,           "[sql hive] RLIKE not converted")
    check('CROSS JOIN UNNEST(' in result5,     "[sql hive] LATERAL VIEW EXPLODE not converted")
    check('-- Raw Hive SQL file' in result5,   "[sql hive] SQL comment was mangled")
    # New rules
    check("CASE WHEN priority = 'H' THEN 'High'" in result5,
          "[sql hive] DECODE not converted to CASE")
    check('AT TIME ZONE' in result5,
          "[sql hive] FROM_UTC_TIMESTAMP not converted")
    check('TODO' in result5 and 'SEMI JOIN' in result5,
          "[sql hive] LEFT SEMI JOIN not flagged")

    # ─── TEST 6: --!null .sql → no conversion ────────────────────────
    print(f"\n{SEP}")
    print("  TEST 6: --!null .sql → no conversion")
    print(SEP)

    result6, report6 = converter.convert_file(SAMPLE_NULL_SQL, "etl_null.sql")
    print(f"  Dialect: {report6.dialect}")
    print(f"  Skipped: {report6.skipped}")

    check(report6.skipped is True,            "[null sql] file was not skipped")
    check(result6 == SAMPLE_NULL_SQL,         "[null sql] file content was modified")
    check('--!null' in result6,               "[null sql] directive was changed")
    check('NVL(a, 0)' in result6,             "[null sql] SQL was modified")
    check('CAST(b AS STRING)' in result6,     "[null sql] SQL was modified")

    # ─── RESULTS ─────────────────────────────────────────────────────
    print(f"\n{SEP}")
    if all_errors:
        print("  FAILURES:")
        for e in all_errors:
            print(f"    ✗ {e}")
        print(SEP)
        sys.exit(1)
    else:
        print("  ✅ TEST 1 PASSED – --!impala .js  → --!trino  (embedded SQL → Trino)")
        print("  ✅ TEST 2 PASSED – --!hive   .js  → --!presto (embedded SQL → Presto)")
        print("  ✅ TEST 3 PASSED – --!null   .js  → skipped")
        print("  ✅ TEST 4 PASSED – --!impala .sql → --!trino  (raw SQL → Trino)")
        print("  ✅ TEST 5 PASSED – --!hive   .sql → --!presto (raw SQL → Presto)")
        print("  ✅ TEST 6 PASSED – --!null   .sql → skipped")
        print(SEP + "\n")


if __name__ == "__main__":
    main()
