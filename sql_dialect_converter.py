#!/usr/bin/env python3
"""
SQL Dialect Converter for Nashorn JavaScript and Raw SQL Files
===============================================================
Reads a dialect label near the top of each file:

    --!impala   →  converts Impala SQL to Trino,  relabels --!trino
    --!hive     →  converts Hive  SQL to Presto,  relabels --!presto
    --!null     →  no conversion, file is left untouched

Supported file types:
    .js         →  Nashorn JS with embedded SQL in string literals
    .sql .hql .hive .ddl .dml  →  raw SQL (rules applied to entire content)

All JavaScript / Nashorn scripting is preserved exactly as-is;
only the SQL content is rewritten.

Usage:
    python sql_dialect_converter.py input.js  -o output.js
    python sql_dialect_converter.py input.sql -o output.sql
    python sql_dialect_converter.py input_dir/ -o output_dir/ --recursive
    python sql_dialect_converter.py input.sql --dry-run
    python sql_dialect_converter.py --self-test
"""

import re
import os
import sys
import argparse
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ===========================================================================
#  DATA TYPES
# ===========================================================================

@dataclass
class StringLiteral:
    """A JS string literal found in the source."""
    start: int
    end: int
    quote: str
    raw: str
    unescaped: str
    is_sql: bool = False
    converted: str = ""


@dataclass
class ConversionReport:
    file: str
    dialect: str = ""
    target: str = ""
    strings_found: int = 0
    strings_changed: int = 0
    rules_applied: list = field(default_factory=list)
    skipped: bool = False


# ===========================================================================
#  DIALECT DETECTION
# ===========================================================================

# Matches the label in JS line-comments:  // --!impala   or  // --!hive  etc.
# Also matches inside string literals or standalone comment lines.
_DIALECT_LABEL_RE = re.compile(
    r'--!(impala|hive|null|trino|presto)\b',
    re.IGNORECASE
)

# Map source dialect → (target dialect, label to write)
DIALECT_MAP = {
    "impala": ("trino",  "--!trino"),
    "hive":   ("presto", "--!presto"),
}


def detect_dialect(source: str, scan_lines: int = 30) -> Optional[re.Match]:
    """
    Scan the first *scan_lines* lines of source for a --!<dialect> label.
    Returns the regex Match object (so we know position + dialect), or None.
    """
    # Only scan the head of the file
    lines = source.split('\n', scan_lines)
    head = '\n'.join(lines[:scan_lines])
    return _DIALECT_LABEL_RE.search(head)


# ===========================================================================
#  BASE CONVERTER – shared rule engine
# ===========================================================================

class SQLConverter:
    """Base class: applies an ordered list of regex rewrite rules."""

    def __init__(self):
        self.rules = self._build_rules()

    def _build_rules(self):
        raise NotImplementedError

    def convert(self, sql: str) -> tuple[str, list[str]]:
        applied = []
        result = sql
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


# ===========================================================================
#  IMPALA → TRINO  RULES
# ===========================================================================

class ImpalaToTrinoConverter(SQLConverter):

    def _build_rules(self):
        r = []

        # ── Impala-only statements ─────────────────────────────────────
        _add(r, "Remove COMPUTE STATS",
             r"(?m)^\s*COMPUTE\s+(?:INCREMENTAL\s+)?STATS\s+[^;]*;?\s*$",
             "/* REMOVED: COMPUTE STATS (not needed in Trino) */")
        _add(r, "Remove INVALIDATE METADATA",
             r"(?m)^\s*INVALIDATE\s+METADATA\s*[^;]*;?\s*$",
             "/* REMOVED: INVALIDATE METADATA (not needed in Trino) */")
        _add(r, "Remove REFRESH",
             r"(?m)^\s*REFRESH\s+[^;]*;?\s*$",
             "/* REMOVED: REFRESH (not needed in Trino) */")

        # ── Data types ─────────────────────────────────────────────────
        _add(r, "CAST(... AS STRING) → CAST(... AS VARCHAR)",
             r"\bCAST\s*\(\s*([^)]+?)\s+AS\s+STRING\s*\)",
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
        _add(r, "Remove STORED AS ...",
             r"\bSTORED\s+AS\s+(?:PARQUET|PARQUETFILE|TEXTFILE|RCFILE|"
             r"SEQUENCEFILE|AVRO|ORC|JSONFILE)\b",
             "/* \\g<0> – adjust for Trino connector */")
        _add(r, "Remove ROW FORMAT DELIMITED ...",
             r"\bROW\s+FORMAT\s+DELIMITED\s+FIELDS\s+TERMINATED\s+BY\s+'[^']*'"
             r"(?:\s+LINES\s+TERMINATED\s+BY\s+'[^']*')?",
             "/* \\g<0> – adjust for Trino connector */")
        _add(r, "Remove LOCATION",
             r"\bLOCATION\s+'[^']*'",
             "/* \\g<0> – adjust for Trino connector */")
        _add(r, "Remove TBLPROPERTIES",
             r"\bTBLPROPERTIES\s*\([^)]*\)",
             "/* \\g<0> – adjust for Trino connector */")
        _add(r, "Remove SORT BY",
             r"\bSORT\s+BY\s*\([^)]*\)",
             "/* \\g<0> – adjust for Trino connector */")

        # ── INSERT OVERWRITE ───────────────────────────────────────────
        _add(r, "INSERT OVERWRITE → INSERT INTO",
             r"\bINSERT\s+OVERWRITE\s+(?:TABLE\s+)?(\S+)",
             r"/* Was INSERT OVERWRITE – truncate first in Trino */ INSERT INTO \1")

        # ── Functions ──────────────────────────────────────────────────
        _add(r, "NVL → COALESCE",
             r"\bNVL\s*\(", "COALESCE(")
        _add(r, "NVL2(e,a,b) → IF(e IS NOT NULL, a, b)",
             r"\bNVL2\s*\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"IF({m.group(1).strip()} IS NOT NULL, "
                        f"{m.group(2).strip()}, {m.group(3).strip()})")
        _add(r, "IFNULL → COALESCE",
             r"\bIFNULL\s*\(", "COALESCE(")
        _add(r, "GROUP_CONCAT → ARRAY_JOIN(ARRAY_AGG(...))",
             r"\bGROUP_CONCAT\s*\(\s*([^,)]+?)\s*(?:,\s*'([^']*)'\s*)?\)",
             lambda m: f"ARRAY_JOIN(ARRAY_AGG({m.group(1).strip()}), "
                        f"'{m.group(2) if m.group(2) else ','}')")

        # Date / time
        _add(r, "UNIX_TIMESTAMP() → TO_UNIXTIME(NOW())",
             r"\bUNIX_TIMESTAMP\s*\(\s*\)", "TO_UNIXTIME(NOW())")
        _add(r, "UNIX_TIMESTAMP(expr) → TO_UNIXTIME(",
             r"\bUNIX_TIMESTAMP\s*\(", "TO_UNIXTIME(")
        _add(r, "FROM_TIMESTAMP → DATE_FORMAT",
             r"\bFROM_TIMESTAMP\s*\(", "DATE_FORMAT(")
        _add(r, "TO_DATE(expr) → CAST(expr AS DATE)",
             r"\bTO_DATE\s*\(\s*([^)]+?)\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS DATE)")
        _add(r, "DATEDIFF(a,b) → DATE_DIFF('day', b, a)",
             r"\bDATEDIFF\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"DATE_DIFF('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_ADD(d, n) → DATE_ADD('day', n, d)",
             r"\bDATE_ADD\s*\(\s*([^,]+?)\s*,\s*(?:INTERVAL\s+)?([^)]+?)\s*\)",
             lambda m: f"DATE_ADD('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_SUB(d, n) → DATE_ADD('day', -n, d)",
             r"\bDATE_SUB\s*\(\s*([^,]+?)\s*,\s*(?:INTERVAL\s+)?([^)]+?)\s*\)",
             lambda m: f"DATE_ADD('day', -({m.group(2).strip()}), {m.group(1).strip()})")
        _add(r, "ADD_MONTHS(d, n) → DATE_ADD('month', n, d)",
             r"\bADD_MONTHS\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"DATE_ADD('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "MONTHS_BETWEEN(a,b) → DATE_DIFF('month', b, a)",
             r"\bMONTHS_BETWEEN\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"DATE_DIFF('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "TRUNC(d, fmt) → DATE_TRUNC(fmt, d)",
             r"\bTRUNC\s*\(\s*([^,]+?)\s*,\s*'([^']+)'\s*\)",
             lambda m: f"DATE_TRUNC('{m.group(2).strip().lower()}', {m.group(1).strip()})")
        _add(r, "CURRENT_TIMESTAMP() → NOW()",
             r"\bCURRENT_TIMESTAMP\s*\(\s*\)", "NOW()")

        # String
        _add(r, "STRLEFT(s,n) → SUBSTR(s,1,n)",
             r"\bSTRLEFT\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"SUBSTR({m.group(1).strip()}, 1, {m.group(2).strip()})")
        _add(r, "STRRIGHT(s,n) → SUBSTR(s,-n)",
             r"\bSTRRIGHT\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"SUBSTR({m.group(1).strip()}, -{m.group(2).strip()})")
        _add(r, "INSTR → STRPOS",
             r"\bINSTR\s*\(", "STRPOS(")

        # Math
        _add(r, "FNV_HASH → XXHASH64",
             r"\bFNV_HASH\s*\(", "XXHASH64(")
        _add(r, "PMOD(a,b) → ((a%b)+b)%b",
             r"\bPMOD\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"(({m.group(1).strip()} % {m.group(2).strip()}) "
                        f"+ {m.group(2).strip()}) % {m.group(2).strip()}")

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

    def _build_rules(self):
        r = []

        # ── Hive-only statements ───────────────────────────────────────
        _add(r, "Remove MSCK REPAIR TABLE",
             r"(?m)^\s*MSCK\s+REPAIR\s+TABLE\s+[^;]*;?\s*$",
             "/* REMOVED: MSCK REPAIR TABLE (not needed in Presto) */")
        _add(r, "Remove ANALYZE TABLE",
             r"(?m)^\s*ANALYZE\s+TABLE\s+[^;]*;?\s*$",
             "/* REMOVED: ANALYZE TABLE (not needed in Presto) */")

        # ── Data types ─────────────────────────────────────────────────
        _add(r, "CAST(... AS STRING) → CAST(... AS VARCHAR)",
             r"\bCAST\s*\(\s*([^)]+?)\s+AS\s+STRING\s*\)",
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
        _add(r, "Remove STORED AS ...",
             r"\bSTORED\s+AS\s+(?:PARQUET|PARQUETFILE|TEXTFILE|RCFILE|"
             r"SEQUENCEFILE|AVRO|ORC|JSONFILE)\b",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove ROW FORMAT DELIMITED ...",
             r"\bROW\s+FORMAT\s+DELIMITED\s+FIELDS\s+TERMINATED\s+BY\s+'[^']*'"
             r"(?:\s+LINES\s+TERMINATED\s+BY\s+'[^']*')?",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove ROW FORMAT SERDE ...",
             r"\bROW\s+FORMAT\s+SERDE\s+'[^']*'(?:\s+WITH\s+SERDEPROPERTIES\s*\([^)]*\))?",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove LOCATION",
             r"\bLOCATION\s+'[^']*'",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove TBLPROPERTIES",
             r"\bTBLPROPERTIES\s*\([^)]*\)",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove CLUSTERED BY ... INTO n BUCKETS",
             r"\bCLUSTERED\s+BY\s*\([^)]*\)\s*(?:SORTED\s+BY\s*\([^)]*\)\s*)?"
             r"INTO\s+\d+\s+BUCKETS",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove SORT BY",
             r"\bSORT\s+BY\s*\([^)]*\)",
             "/* \\g<0> – adjust for Presto connector */")
        _add(r, "Remove DISTRIBUTE BY",
             r"\bDISTRIBUTE\s+BY\b[^;)]*",
             "/* \\g<0> – adjust for Presto connector */")

        # ── INSERT OVERWRITE ───────────────────────────────────────────
        _add(r, "INSERT OVERWRITE → INSERT INTO",
             r"\bINSERT\s+OVERWRITE\s+(?:TABLE\s+)?(\S+)",
             r"/* Was INSERT OVERWRITE – truncate first in Presto */ INSERT INTO \1")

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
        _add(r, "CONCAT_WS stays the same (both support it)",
             r"placeholder_no_match_xyzzy", "")  # doc-only

        # String
        _add(r, "INSTR → STRPOS",
             r"\bINSTR\s*\(", "STRPOS(")
        _add(r, "LENGTH → LENGTH (same)", r"placeholder_len_xyzzy", "")
        _add(r, "LOCATE(needle,hay) → STRPOS(hay,needle)",
             r"\bLOCATE\s*\(\s*([^,]+?)\s*,\s*([^),]+?)\s*\)",
             lambda m: f"STRPOS({m.group(2).strip()}, {m.group(1).strip()})")

        # Regex
        _add(r, "RLIKE → REGEXP_LIKE",
             r"\b(\S+)\s+RLIKE\s+'([^']+)'",
             lambda m: f"REGEXP_LIKE({m.group(1).strip()}, '{m.group(2)}')")
        _add(r, "REGEXP_EXTRACT(s,pat,idx) stays same in Presto",
             r"placeholder_regexp_xyzzy", "")

        # Date / time
        _add(r, "UNIX_TIMESTAMP() → TO_UNIXTIME(NOW())",
             r"\bUNIX_TIMESTAMP\s*\(\s*\)", "TO_UNIXTIME(NOW())")
        _add(r, "UNIX_TIMESTAMP(expr) → TO_UNIXTIME(",
             r"\bUNIX_TIMESTAMP\s*\(", "TO_UNIXTIME(")
        _add(r, "FROM_UNIXTIME stays same", r"placeholder_fux_xyzzy", "")
        _add(r, "TO_DATE(expr) → CAST(expr AS DATE)",
             r"\bTO_DATE\s*\(\s*([^)]+?)\s*\)",
             lambda m: f"CAST({m.group(1).strip()} AS DATE)")
        _add(r, "DATEDIFF(a,b) → DATE_DIFF('day', b, a)",
             r"\bDATEDIFF\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"DATE_DIFF('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_ADD(d, n) → DATE_ADD('day', n, d)",
             r"\bDATE_ADD\s*\(\s*([^,]+?)\s*,\s*(?:INTERVAL\s+)?([^)]+?)\s*\)",
             lambda m: f"DATE_ADD('day', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "DATE_SUB(d, n) → DATE_ADD('day', -n, d)",
             r"\bDATE_SUB\s*\(\s*([^,]+?)\s*,\s*(?:INTERVAL\s+)?([^)]+?)\s*\)",
             lambda m: f"DATE_ADD('day', -({m.group(2).strip()}), {m.group(1).strip()})")
        _add(r, "ADD_MONTHS(d, n) → DATE_ADD('month', n, d)",
             r"\bADD_MONTHS\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"DATE_ADD('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "MONTHS_BETWEEN(a,b) → DATE_DIFF('month', b, a)",
             r"\bMONTHS_BETWEEN\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"DATE_DIFF('month', {m.group(2).strip()}, {m.group(1).strip()})")
        _add(r, "TRUNC(d, fmt) → DATE_TRUNC(fmt, d)",
             r"\bTRUNC\s*\(\s*([^,]+?)\s*,\s*'([^']+)'\s*\)",
             lambda m: f"DATE_TRUNC('{m.group(2).strip().lower()}', {m.group(1).strip()})")
        _add(r, "CURRENT_TIMESTAMP() → NOW()",
             r"\bCURRENT_TIMESTAMP\s*\(\s*\)", "NOW()")

        # Aggregate / analytic
        _add(r, "PERCENTILE_APPROX → APPROX_PERCENTILE",
             r"\bPERCENTILE_APPROX\s*\(", "APPROX_PERCENTILE(")

        # Math
        _add(r, "PMOD(a,b) → ((a%b)+b)%b",
             r"\bPMOD\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
             lambda m: f"(({m.group(1).strip()} % {m.group(2).strip()}) "
                        f"+ {m.group(2).strip()}) % {m.group(2).strip()}")

        # LATERAL VIEW EXPLODE → CROSS JOIN UNNEST
        _add(r, "LATERAL VIEW EXPLODE(col) t AS c → CROSS JOIN UNNEST(col) AS t(c)",
             r"\bLATERAL\s+VIEW\s+EXPLODE\s*\(\s*([^)]+?)\s*\)\s+(\w+)\s+AS\s+(\w+)",
             lambda m: f"CROSS JOIN UNNEST({m.group(1).strip()}) "
                        f"AS {m.group(2).strip()}({m.group(3).strip()})")
        _add(r, "LATERAL VIEW POSEXPLODE(col) t AS p,c → CROSS JOIN UNNEST(col) WITH ORDINALITY AS t(c,p)",
             r"\bLATERAL\s+VIEW\s+POSEXPLODE\s*\(\s*([^)]+?)\s*\)\s+(\w+)\s+AS\s+(\w+)\s*,\s*(\w+)",
             lambda m: f"CROSS JOIN UNNEST({m.group(1).strip()}) WITH ORDINALITY "
                        f"AS {m.group(2).strip()}({m.group(4).strip()}, {m.group(3).strip()})")

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


def find_sql_strings(source: str) -> list[StringLiteral]:
    all_strings: list[StringLiteral] = []
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
    groups: list[list[int]] = []
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

    def convert_file(self, source: str,
                     filepath: str = "<stdin>") -> tuple[str, ConversionReport]:
        report = ConversionReport(file=filepath)

        # 1. Detect dialect label
        label_match = detect_dialect(source)
        if not label_match:
            log.warning(f"{filepath}: no --!<dialect> label found – skipping")
            report.skipped = True
            report.dialect = "unknown"
            return source, report

        dialect = label_match.group(1).lower()
        report.dialect = dialect

        # 2. Check if conversion is needed
        if dialect == "null":
            log.info(f"{filepath}: --!null label – skipping (no conversion)")
            report.skipped = True
            return source, report

        if dialect in ("trino", "presto"):
            log.info(f"{filepath}: already labelled --!{dialect} – skipping")
            report.skipped = True
            return source, report

        if dialect not in DIALECT_MAP:
            log.warning(f"{filepath}: unknown dialect --!{dialect} – skipping")
            report.skipped = True
            return source, report

        target, new_label = DIALECT_MAP[dialect]
        report.target = target

        # 3. Build the appropriate converter
        converter_cls = _CONVERTERS[dialect]
        converter = converter_cls()

        # 4. Choose conversion strategy based on file type
        ext = Path(filepath).suffix.lower()
        if ext in self.RAW_SQL_EXTENSIONS:
            result = self._convert_raw_sql(source, converter, report)
        else:
            result = self._convert_embedded_sql(source, converter, report)

        # 5. Replace the dialect label
        old_label = label_match.group(0)   # e.g. "--!impala"
        result = result.replace(old_label, new_label, 1)

        return result, report

    @staticmethod
    def _convert_raw_sql(source: str, converter: SQLConverter,
                         report: ConversionReport) -> str:
        """
        For .sql / .hql / .ddl files: apply conversion rules directly
        to the entire file content (the SQL is raw text, not inside
        JS string literals).
        """
        report.strings_found = 1   # treat whole file as one SQL block
        converted, rules = converter.convert(source)
        if rules:
            report.strings_changed = 1
            report.rules_applied.extend(rules)
            return converted
        return source

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
            if not lit.converted or lit.converted == lit.unescaped:
                continue
            escaped = _escape_js(lit.converted, lit.quote)
            replacement = f'{lit.quote}{escaped}{lit.quote}'
            result = result[:lit.start] + replacement + result[lit.end:]

        return result


# ===========================================================================
#  CLI
# ===========================================================================

def process_file(input_path: str, output_path: Optional[str],
                 dry_run: bool = False) -> ConversionReport:
    converter = FileConverter()
    with open(input_path, 'r', encoding='utf-8') as f:
        source = f.read()

    result, report = converter.convert_file(source, filepath=input_path)

    if report.skipped:
        if not dry_run and output_path:
            # Still copy the file so output directory is complete
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(result)
    elif dry_run:
        log.info(f"[DRY RUN] {report.file}: --!{report.dialect} → --!{report.target}  "
                 f"{report.strings_found} SQL strings, {report.strings_changed} changed")
        for rule in sorted(set(report.rules_applied)):
            log.info(f"  → {rule}")
    else:
        out = output_path or input_path
        os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            f.write(result)
        log.info(f"Converted {report.file} → {out}  "
                 f"[--!{report.dialect} → --!{report.target}]  "
                 f"({report.strings_changed}/{report.strings_found} strings changed)")

    return report


def process_directory(input_dir: str, output_dir: str,
                      recursive: bool, dry_run: bool) -> list[ConversionReport]:
    reports = []
    extensions = ('*.js', '*.sql', '*.hql', '*.hive', '*.ddl', '*.dml')
    for ext in extensions:
        pattern = f'**/{ext}' if recursive else ext
        for fpath in sorted(Path(input_dir).glob(pattern)):
            rel = fpath.relative_to(input_dir)
            out = str(Path(output_dir) / rel)
            reports.append(process_file(str(fpath), out, dry_run))
    return reports


def main():
    parser = argparse.ArgumentParser(
        description="Convert SQL dialects in .js and .sql files based on "
                    "--!impala / --!hive / --!null labels."
    )
    parser.add_argument("input", nargs='?', help="Input .js/.sql file or directory")
    parser.add_argument("-o", "--output", help="Output file or directory")
    parser.add_argument("-r", "--recursive", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Preview changes without writing")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true",
                        help="Run built-in self-test")
    args = parser.parse_args()

    if args.self_test or not args.input:
        _self_test()
        return

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    inp = args.input
    if os.path.isdir(inp):
        if not args.output:
            log.error("Output directory (-o) required for directory input.")
            sys.exit(1)
        reports = process_directory(inp, args.output, args.recursive, args.dry_run)
        converted = [r for r in reports if not r.skipped]
        skipped   = [r for r in reports if r.skipped]
        log.info(f"\nProcessed {len(reports)} files: "
                 f"{len(converted)} converted, {len(skipped)} skipped.")
    elif os.path.isfile(inp):
        process_file(inp, args.output, args.dry_run)
    else:
        log.error(f"Not found: {inp}")
        sys.exit(1)


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
       GROUP_CONCAT(`status`)
FROM db.source
WHERE DATEDIFF(NOW(), created_ts) <= 30;

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
    DATEDIFF(NOW(), created_at)
FROM db.events
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

    # Label
    check('--!trino' in result,                "Label not changed to --!trino")
    check('--!impala' not in result,           "Old --!impala label still present")

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
    check('TO_UNIXTIME(NOW())' in result,     "[impala] UNIX_TIMESTAMP not converted")
    check('XXHASH64(' in result,              "[impala] FNV_HASH not converted")
    check("DATE_ADD('day'" in result,         "[impala] DATE_ADD not converted")
    check("DATE_DIFF('month'" in result,      "[impala] MONTHS_BETWEEN not converted")
    check('REMOVED' in result and 'COMPUTE STATS' in result,
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

    # Label
    check('--!presto' in result2,              "Label not changed to --!presto")
    check('--!hive' not in result2,            "Old --!hive label still present")

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
    check('TO_UNIXTIME(NOW())' in result2,     "[hive] UNIX_TIMESTAMP not converted")
    check("DATE_DIFF('day'" in result2,        "[hive] DATEDIFF not converted")
    check('REGEXP_LIKE(' in result2,           "[hive] RLIKE not converted")
    check('CROSS JOIN UNNEST(' in result2,     "[hive] LATERAL VIEW EXPLODE not converted")
    check('STRPOS(haystack_col' in result2,    "[hive] LOCATE not converted")
    check('APPROX_PERCENTILE(' in result2,     "[hive] PERCENTILE_APPROX not converted")
    check('STORED AS ORC' not in result2 or 'adjust for Presto' in result2,
          "[hive] STORED AS not handled")

    # ─── TEST 3: --!null .js → no conversion ─────────────────────────
    print(f"\n{SEP}")
    print("  TEST 3: --!null .js → no conversion")
    print(SEP)

    result3, report3 = converter.convert_file(SAMPLE_NULL, "null_file.js")
    print(f"  Dialect: {report3.dialect}")
    print(f"  Skipped: {report3.skipped}")

    check(report3.skipped is True,            "[null js] file was not skipped")
    check(result3 == SAMPLE_NULL,             "[null js] file content was modified")
    check('--!null' in result3,               "[null js] label was changed")
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

    check('--!trino' in result4,                "[sql impala] label not changed to --!trino")
    check('--!impala' not in result4,           "[sql impala] old label still present")
    check('VARCHAR' in result4,                 "[sql impala] STRING not converted")
    check('REAL' in result4,                    "[sql impala] FLOAT not converted")
    check('COALESCE(' in result4,               "[sql impala] NVL not converted")
    check('CAST(score AS VARCHAR)' in result4,  "[sql impala] CAST AS STRING not converted")
    check('CAST(created_ts AS DATE)' in result4,"[sql impala] TO_DATE not converted")
    check("DATE_DIFF('day'" in result4,         "[sql impala] DATEDIFF not converted")
    check('INSERT INTO' in result4,             "[sql impala] INSERT OVERWRITE not converted")
    check('ARRAY_JOIN(ARRAY_AGG(' in result4,   "[sql impala] GROUP_CONCAT not converted")
    check('REMOVED' in result4 and 'COMPUTE STATS' in result4,
          "[sql impala] COMPUTE STATS not removed")
    check('STORED AS PARQUET' not in result4 or 'adjust for Trino' in result4,
          "[sql impala] STORED AS not handled")
    # SQL comments must still be there
    check('-- Raw Impala SQL file' in result4,  "[sql impala] SQL comment was mangled")

    # ─── TEST 5: --!hive .sql → --!presto (raw SQL) ──────────────────
    print(f"\n{SEP}")
    print("  TEST 5: --!hive .sql → --!presto (raw SQL file)")
    print(SEP)

    result5, report5 = converter.convert_file(SAMPLE_HIVE_SQL, "etl_hive.sql")
    print(f"  Dialect: {report5.dialect} → {report5.target}")
    print(f"  Strings changed: {report5.strings_changed}")
    for rule in sorted(set(report5.rules_applied)):
        print(f"    ✓ {rule}")

    check('--!presto' in result5,              "[sql hive] label not changed to --!presto")
    check('--!hive' not in result5,            "[sql hive] old label still present")
    check('ARRAY_AGG(' in result5,             "[sql hive] COLLECT_LIST not converted")
    check('CARDINALITY(' in result5,           "[sql hive] SIZE not converted")
    check('TO_UNIXTIME(NOW())' in result5,     "[sql hive] UNIX_TIMESTAMP not converted")
    check("DATE_DIFF('day'" in result5,        "[sql hive] DATEDIFF not converted")
    check('REGEXP_LIKE(' in result5,           "[sql hive] RLIKE not converted")
    check('CROSS JOIN UNNEST(' in result5,     "[sql hive] LATERAL VIEW EXPLODE not converted")
    check('-- Raw Hive SQL file' in result5,   "[sql hive] SQL comment was mangled")

    # ─── TEST 6: --!null .sql → no conversion ────────────────────────
    print(f"\n{SEP}")
    print("  TEST 6: --!null .sql → no conversion")
    print(SEP)

    result6, report6 = converter.convert_file(SAMPLE_NULL_SQL, "etl_null.sql")
    print(f"  Dialect: {report6.dialect}")
    print(f"  Skipped: {report6.skipped}")

    check(report6.skipped is True,            "[null sql] file was not skipped")
    check(result6 == SAMPLE_NULL_SQL,         "[null sql] file content was modified")
    check('--!null' in result6,               "[null sql] label was changed")
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
