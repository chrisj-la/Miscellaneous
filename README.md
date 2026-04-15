# SQL Dialect Converter

Converts Impala SQL to Trino and Hive SQL to Presto within Nashorn JavaScript
(`.js`) and raw SQL (`.sql`, `.hql`, `.hive`, `.ddl`, `.dml`) files. All
JavaScript logic, control-flow directives, variable substitution patterns, and
eval blocks are preserved unchanged — only the SQL content is rewritten.

---

## Prerequisites

- **Python 3.6+** (no external packages required — standard library only)

---

## How It Works

The converter performs four steps on each file:

1. **Dialect detection** — Scans the first 30 lines for a `--!impala`,
   `--!hive`, or `--!null` dialect directive (typically inside a SQL comment
   or JS comment). This directive determines which conversion ruleset to
   apply and what the output directive should be.

2. **Segmentation** — For raw SQL files, the converter splits the file into
   protected and convertible regions. Protected regions include
   `--!javascript`/`--!endjavascript` blocks, `--!eval` lines, and all
   control-flow directives (`--!if`, `--!else`, `--!endif`, `--!forquery`,
   `--!do`, `--!done`, etc.). For `.js` files, it scans for SQL inside
   JavaScript string literals using heuristics (SQL keywords, variable names
   like `sql`/`query`/`stmt`, and JDBC method calls), propagating SQL
   detection across `+`-concatenated string chains.

3. **Conversion** — Applies an ordered set of regex-based rewrite rules to
   each SQL region. Rules handle nested function calls via balanced-parenthesis
   patterns that support up to two levels of nesting with embedded string
   literals. Complex rewrites like `DECODE` with variable-length arguments
   use a dedicated pre-processing pass with full argument parsing.

4. **Directive replacement** — Swaps the dialect directive to reflect the
   target dialect (e.g. `--!impala` → `--!trino`).

### Dialect routing

| Source Directive | Target Directive | Action |
|---|---|---|
| `--!impala` | `--!trino` | Impala SQL → Trino SQL |
| `--!hive` | `--!presto` | Hive SQL → Presto SQL |
| `--!null` | *(unchanged)* | File skipped entirely |
| `--!trino` / `--!presto` | *(unchanged)* | Already converted, skipped |
| *(no directive)* | — | Warning logged, file skipped |

### File type handling

| Extension | Mode | How SQL is found |
|---|---|---|
| `.js` | Embedded SQL | SQL inside JS string literals (`"..."` / `'...'`) |
| `.sql`, `.hql`, `.hive`, `.ddl`, `.dml` | Raw SQL | Entire file content (minus protected blocks) |

---

## Usage

### Single file
```bash
python impala-hive_to_trino-presto.py input.sql -o output.sql
python impala-hive_to_trino-presto.py script.js -o converted.js
```

### Directory (all supported extensions)
```bash
python impala-hive_to_trino-presto.py input_dir/ -o output_dir/
python impala-hive_to_trino-presto.py input_dir/ -o output_dir/ --recursive
```

### Preview changes without writing
```bash
python impala-hive_to_trino-presto.py input.sql --dry-run
```

### Run built-in self-test
```bash
python impala-hive_to_trino-presto.py --self-test
```

### Full option list
```
python impala-hive_to_trino-presto.py [-h] [-o OUTPUT] [-r] [-n] [-v] [--annotate] [--self-test] [input]

positional arguments:
  input                 Input .js/.sql file or directory

options:
  -o, --output          Output file or directory
  -r, --recursive       Recursively process directories
  -n, --dry-run         Preview changes without writing
  -v, --verbose         Enable debug-level logging
  --annotate            Add comments showing removed/replaced code
  --self-test           Run built-in self-test (6 test cases)
```

By default, the script produces **clean output**: removed statements are
deleted silently, replaced code appears without explanatory comments, and
DDL clauses like `LOCATION '...'` and `TBLPROPERTIES(...)` are stripped with
no trace, while `STORED AS PARQUET` is converted to `WITH (format = 'PARQUET')`.
This makes the converted files ready to run without manual cleanup of comment
noise.

The `--annotate` flag switches to **verbose output**: every removal or
replacement is wrapped in a comment showing what was there before and why
it changed. This is useful when reviewing conversions for the first time or
auditing what the script did.

`TODO` comments (semi/anti joins, unsupported system functions) are **always
included** regardless of whether `--annotate` is used, since they mark code
that requires manual completion.

### Review report

After every conversion run (unless `--dry-run` is used), the script
generates a `conversion_review_report.txt` file in the same directory as the
script itself. This report contains two sections:

1. **Conversion flags** — items requiring manual attention, with line
   numbers and categories.
2. **User-defined functions** — a consolidated list of every function call
   not recognized as a built-in, with file locations and occurrence counts.

Flag categories:

| Category | Meaning |
|---|---|
| `TODO` | Requires manual rewrite (semi/anti joins, unsupported functions) |
| `REMOVED` | Statement was removed — may need a replacement |
| `INSERT OVERWRITE` | Converted to INSERT INTO — may need session property or truncate |
| `DDL ADJUSTMENT` | DDL clause like LOCATION or TBLPROPERTIES commented out — needs connector syntax |

UDF detection works by comparing every `function_name(` pattern in SQL
regions against a whitelist of ~200 known built-in functions across Impala,
Hive, Trino, and Presto. Anything not in the whitelist is reported as a
potential UDF. Schema-qualified calls (e.g. `my_db.custom_func()`) are
always flagged. Functions inside `--!javascript` blocks, `--!eval` lines,
and SQL comments are excluded from scanning.

Example report output:
```
==============================================================================
  SQL DIALECT CONVERSION — REVIEW REPORT
==============================================================================

Files processed:        5
Files converted:        4
Files skipped:          1
Files with flags:       1
Total flags:            2
Files with UDFs:        3
Unique UDFs detected:   7
Total UDF references:   9

Flags by category:
  REMOVED: 1
  TODO: 1

------------------------------------------------------------------------------
  DETAILED FLAGS BY FILE
------------------------------------------------------------------------------

etl_impala.sql  [--!impala → --!trino]
  Line    15  [TODO]  TODO: Rewrite LEFT SEMI JOIN to WHERE EXISTS (...)
  Line    18  [REMOVED]  REMOVED: COMPUTE STATS (not needed in Trino)

------------------------------------------------------------------------------
  USER-DEFINED FUNCTIONS DETECTED
------------------------------------------------------------------------------

7 unique function(s) not recognized as built-in, found in 3 file(s).
These may be user-defined functions that require manual porting to Trino/Presto.

  analytics_db.parse_event_json()  (1 occurrence(s))
    etl_hive.sql  line 5

  calc_risk_score()  (3 occurrence(s))
    etl_impala.sql  line 9
    etl_impala.sql  line 16
    report.sql  line 3

  format_ssn()  (1 occurrence(s))
    etl_impala.sql  line 10

  geo_distance()  (1 occurrence(s))
    etl_hive.sql  line 6

  mask_pii()  (1 occurrence(s))
    etl_hive.sql  line 7

  my_company.clean_phone()  (1 occurrence(s))
    etl_impala.sql  line 8

==============================================================================
```

---

## Conversions Performed

### Impala → Trino (68 active rules + DECODE pre-pass)

**Statements removed/neutralized:**

| Impala | Trino |
|---|---|
| `COMPUTE STATS t` | Removed (comment inserted) |
| `INVALIDATE METADATA t` | Removed (comment inserted) |
| `REFRESH t` | Removed (comment inserted) |

**Data types:**

| Impala | Trino |
|---|---|
| `STRING` | `VARCHAR` |
| `FLOAT` | `REAL` |
| `INT` | `INTEGER` |
| `TINYINT` | `SMALLINT` |
| `CAST(x AS STRING)` | `CAST(x AS VARCHAR)` |

**DDL / storage clauses:**

| Impala | Trino |
|---|---|
| `STORED AS PARQUET` | `WITH (format = 'PARQUET')` |
| `STORED AS ORC` | `WITH (format = 'ORC')` |
| `STORED AS AVRO` | `WITH (format = 'AVRO')` |
| `STORED AS TEXTFILE` | `WITH (format = 'TEXTFILE')` |

Other DDL clauses are removed (or commented out with `--annotate`):
`ROW FORMAT DELIMITED`, `LOCATION '...'`, `TBLPROPERTIES(...)`, `SORT BY(...)`

**DML:**

| Impala | Trino |
|---|---|
| `INSERT OVERWRITE TABLE t` | `INSERT INTO t` (with comment) |

**Functions — null handling:**

| Impala | Trino |
|---|---|
| `NVL(a, b)` | `COALESCE(a, b)` |
| `NVL2(e, a, b)` | `IF(e IS NOT NULL, a, b)` |
| `IFNULL(a, b)` | `COALESCE(a, b)` |
| `ISNULL(x)` | `(x IS NULL)` |
| `NULLIFZERO(x)` | `NULLIF(x, 0)` |
| `ZEROIFNULL(x)` | `COALESCE(x, 0)` |

**Functions — date / time:**

| Impala | Trino |
|---|---|
| `DATEDIFF(a, b)` | `DATE_DIFF('day', b, a)` (args reversed) |
| `DATE_ADD(d, n)` | `DATE_ADD('day', n, d)` (args reordered) |
| `DATE_SUB(d, n)` | `DATE_ADD('day', -(n), d)` |
| `ADD_MONTHS(d, n)` | `DATE_ADD('month', n, d)` |
| `MONTHS_BETWEEN(a, b)` | `DATE_DIFF('month', b, a)` |
| `TO_DATE(ts)` | `CAST(ts AS DATE)` |
| `TRUNC(d, 'MM')` | `DATE_TRUNC('mm', d)` |
| `UNIX_TIMESTAMP()` | `TO_UNIXTIME(current_timestamp)` |
| `UNIX_TIMESTAMP(expr)` | `TO_UNIXTIME(expr)` |
| `FROM_TIMESTAMP(ts, fmt)` | `DATE_FORMAT(ts, fmt)` |
| `CURRENT_TIMESTAMP()` | `current_timestamp` |
| `NOW()` | `current_timestamp` |
| `FROM_UTC_TIMESTAMP(ts, tz)` | `CAST(ts AS TIMESTAMP) AT TIME ZONE tz` |
| `TO_UTC_TIMESTAMP(ts, tz)` | `CAST(ts AT TIME ZONE tz ...) AT TIME ZONE 'UTC'` |

**Functions — string:**

| Impala | Trino |
|---|---|
| `STRLEFT(s, n)` | `SUBSTR(s, 1, n)` |
| `STRRIGHT(s, n)` | `SUBSTR(s, -n)` |
| `INSTR(hay, needle)` | `STRPOS(hay, needle)` |
| `GROUP_CONCAT(col)` | `ARRAY_JOIN(ARRAY_AGG(col), ',')` |
| `LCASE(s)` | `lower(s)` |
| `UCASE(s)` | `upper(s)` |
| `levenshtein(a, b)` | `levenshtein_distance(a, b)` |
| `BASE64ENCODE(s)` | `to_base64(s)` |
| `BASE64DECODE(s)` | `from_base64(s)` |
| `FIND_IN_SET(s, csv)` | *(no equivalent)* — flagged with TODO |

**Functions — math / aggregate / analytics:**

| Impala | Trino |
|---|---|
| `FNV_HASH(x)` | `XXHASH64(x)` |
| `PMOD(a, b)` | `((a % b) + b) % b` |
| `NDV(col)` | `APPROX_DISTINCT(col)` |
| `APPX_MEDIAN(col)` | `APPROX_PERCENTILE(col, 0.5)` |

**Functions — conditional:**

| Impala | Trino |
|---|---|
| `DECODE(expr, s1, r1, ..., default)` | `CASE WHEN expr = s1 THEN r1 ... ELSE default END` |

**Regex / pattern matching:**

| Impala | Trino |
|---|---|
| `col REGEXP 'pattern'` | `REGEXP_LIKE(col, 'pattern')` |
| `col RLIKE 'pattern'` | `REGEXP_LIKE(col, 'pattern')` |
| `col ILIKE 'pattern'` | `lower(col) LIKE lower('pattern')` |
| `col IREGEXP 'pattern'` | `regexp_like(col, '(?i)pattern')` |

**Join rewrites (flagged with TODO for manual review):**

| Impala | Trino |
|---|---|
| `LEFT SEMI JOIN` | `JOIN` + TODO comment |
| `RIGHT SEMI JOIN` | `JOIN` + TODO comment |
| `LEFT ANTI JOIN` | `LEFT JOIN` + TODO comment |
| `RIGHT ANTI JOIN` | `RIGHT JOIN` + TODO comment |

**Date format specifiers (Java → MySQL/Trino style):**

Applied inside `DATE_FORMAT()` and `DATE_PARSE()` calls.

| Java | MySQL/Trino | Meaning |
|---|---|---|
| `yyyy` | `%Y` | 4-digit year |
| `yy` | `%y` | 2-digit year |
| `MM` | `%m` | Month (01–12) |
| `dd` | `%d` | Day (01–31) |
| `HH` | `%H` | Hour 24h (00–23) |
| `hh` | `%h` | Hour 12h (01–12) |
| `mm` | `%i` | Minute (00–59) |
| `ss` | `%s` | Second (00–59) |
| `a` | `%p` | AM/PM |
| `MMM` | `%b` | Abbreviated month name |
| `MMMM` | `%M` | Full month name |

**System / session functions:**

| Impala | Trino | Notes |
|---|---|---|
| `EFFECTIVE_USER()` | `current_user` | Keyword, no parens |
| `USER()` | `current_user` | Keyword, no parens |
| `LOGGED_IN_USER()` | `current_user` | Keyword, no parens |
| `CURRENT_DATABASE()` | `current_schema` | Keyword, no parens |
| `PID()` | *(no equivalent)* | Flagged with TODO comment |
| `COORDINATOR()` | *(no equivalent)* | Flagged with TODO comment |
| `SLEEP(n)` | *(no equivalent)* | Flagged with TODO comment |

**Identifiers / misc:**

| Impala | Trino |
|---|---|
| `` `column_name` `` | `"column_name"` |
| `/* +SHUFFLE */` hints | Removed |
| `SHOW DATABASES` | `SHOW SCHEMAS` |
| `TABLESAMPLE SYSTEM(n)` | `TABLESAMPLE BERNOULLI(n)` |

---

### Hive → Presto (60 active rules + DECODE pre-pass)

Shares many rules with Impala→Trino. Key differences and Hive-specific rules:

**Statements removed/neutralized:**

| Hive | Presto |
|---|---|
| `MSCK REPAIR TABLE t` | Removed (comment inserted) |
| `ANALYZE TABLE t` | Removed (comment inserted) |

**Additional data types:**

| Hive | Presto |
|---|---|
| `BINARY` | `VARBINARY` |

**Additional DDL clauses (commented out with adjustment note):**

`ROW FORMAT SERDE '...'`, `CLUSTERED BY (...) INTO n BUCKETS`,
`DISTRIBUTE BY ...`

**Collection functions:**

| Hive | Presto |
|---|---|
| `COLLECT_LIST(col)` | `ARRAY_AGG(col)` |
| `COLLECT_SET(col)` | `SET_AGG(col)` |
| `SIZE(collection)` | `CARDINALITY(collection)` |
| `SORT_ARRAY(arr)` | `ARRAY_SORT(arr)` |
| `ARRAY_CONTAINS(arr, val)` | `CONTAINS(arr, val)` |

**String / regex:**

| Hive | Presto |
|---|---|
| `LOCATE(needle, haystack)` | `STRPOS(haystack, needle)` (args reversed) |
| `col RLIKE 'pattern'` | `REGEXP_LIKE(col, 'pattern')` |
| `LCASE(s)` | `lower(s)` |
| `UCASE(s)` | `upper(s)` |
| `levenshtein(a, b)` | `levenshtein_distance(a, b)` |
| `FIND_IN_SET(s, csv)` | *(no equivalent)* — flagged with TODO |

**JSON:**

| Hive | Presto |
|---|---|
| `GET_JSON_OBJECT(json, path)` | `json_extract_scalar(json, path)` |

**Hash / encoding:**

| Hive | Presto |
|---|---|
| `SHA(s)` | `sha1(s)` |
| `SHA2(s, 256)` | `sha256(s)` |
| `SHA2(s, 512)` | `sha512(s)` |
| `BASE64(s)` | `to_base64(s)` |
| `UNBASE64(s)` | `from_base64(s)` |

**Aggregate:**

| Hive | Presto |
|---|---|
| `PERCENTILE_APPROX(col, pct)` | `APPROX_PERCENTILE(col, pct)` |

**LATERAL VIEW → CROSS JOIN UNNEST:**

| Hive | Presto |
|---|---|
| `LATERAL VIEW EXPLODE(col) t AS c` | `CROSS JOIN UNNEST(col) AS t(c)` |
| `LATERAL VIEW POSEXPLODE(col) t AS p, c` | `CROSS JOIN UNNEST(col) WITH ORDINALITY AS t(c, p)` |

**System / session functions:**

| Hive | Presto | Notes |
|---|---|---|
| `LOGGED_IN_USER()` | `current_user` | Keyword, no parens |
| `CURRENT_DATABASE()` | `current_schema` | Keyword, no parens |

All date/time, timezone, null-handling, INSERT OVERWRITE, identifier, join
rewrite, and date format rules are the same as the Impala→Trino converter
(with Presto-appropriate comments).

---

## What Is Preserved (Never Modified)

The following constructs are always left untouched regardless of file type:

- `--!javascript` / `--!endjavascript` blocks (entire block contents)
- `--!eval` lines (JavaScript expressions)
- `--!if` / `--!else` / `--!endif` control flow directives
- `--!forquery` / `--!do` / `--!done` loop directives
- All other `--!` directive lines
- `${?variable}` substitution patterns
- `${ENV.VAR_NAME}` environment variable references
- `mash.get()` and `record.get()` calls
- Java object instantiations (e.g. `new java.util.Date()`)
- `throw` statements and error handling logic
- All JavaScript variable assignments and logic
- SQL comments (`--` and `/* */`)

---

## Known Gaps

These are differences between Impala/Hive and Trino/Presto that the script
**does not convert automatically** because they are either impossible to detect
via regex, require runtime context, or risk producing incorrect results:

### Silent behavioral differences (no error, wrong results)

| Issue | Detail |
|---|---|
| **NULL ordering** | Impala defaults to `NULLS FIRST` in ascending ORDER BY; Trino defaults to `NULLS LAST`. Add explicit `NULLS FIRST` or `NULLS LAST` to every ORDER BY clause. |
| **Integer division** | Trino returns `7/2 = 3` (integer); Impala may return `3.5`. Wrap in `CAST(... AS DOUBLE)` if decimal results are expected. |
| **Decimal precision overflow** | Impala truncates results exceeding DECIMAL(38,n) to at least 6 fractional digits; Trino throws an error. |
| **Array indexing** | Impala arrays are 0-based; Trino arrays are 1-based. The script cannot safely auto-convert `arr[0]` to `arr[1]` because it cannot distinguish array subscripts from other bracket usage. |
| **Implicit type casting** | Impala auto-casts strings to timestamps in comparisons; Trino requires explicit `CAST()` or `DATE`/`TIMESTAMP` literals. |
| **Case sensitivity** | Trino automatically lowercases non-ASCII alphabets, which can break case-sensitive business logic. |
| **Escape characters** | Hive uses `\\\\` for backslash in strings; Trino uses `\\`. |
| **`current_timestamp` timezone** | Impala's `now()` returns `TIMESTAMP` (no timezone); Trino's `current_timestamp` returns `TIMESTAMP WITH TIME ZONE`. The script normalizes `now()` and `CURRENT_TIMESTAMP()` to the canonical `current_timestamp` keyword. This can cause implicit timezone conversions when comparing against or storing into bare `TIMESTAMP` columns. Use `localtimestamp` in Trino if timezone-free behavior is required. |

### Conversions that require manual review

| Issue | What the script does | What you need to do |
|---|---|---|
| **LEFT SEMI JOIN** | Replaces with `JOIN` + inserts a `/* TODO */` comment | Rewrite to `WHERE EXISTS (SELECT 1 FROM t2 WHERE ...)` |
| **LEFT ANTI JOIN** | Replaces with `LEFT JOIN` + inserts a `/* TODO */` comment | Rewrite to `WHERE NOT EXISTS (...)` or add `WHERE t2.key IS NULL` |
| **RIGHT SEMI / ANTI JOIN** | Same pattern | Same approach, reversed |
| **INSERT OVERWRITE** | Replaces with `INSERT INTO` + comment | Set session property `insert_existing_partitions_behavior = 'OVERWRITE'` or add a `DELETE` / `TRUNCATE` before the `INSERT` |
| **LOCATION / TBLPROPERTIES** | Comments out with adjustment note | Rewrite using Trino's `WITH (external_location = '...')` or table properties syntax |
| **COMPUTE STATS** | Removes (comment inserted) | Replace with `ANALYZE table` if statistics are needed |
| **MSCK REPAIR TABLE** | Removes (comment inserted) | Replace with `CALL system.sync_partition_metadata(schema, table, 'FULL')` |

### Not supported (requires rewriting outside the script)

| Issue | Detail |
|---|---|
| **Hive UDFs** | Custom `GenericUDF` classes cannot run in Trino/Presto. Rewrite as Trino plugin functions or inline SQL UDFs. |
| **Custom SerDes** | Third-party SerDe JARs are not supported in Trino. Convert to native formats (Parquet, ORC) or use Regex. |
| **TRANSFORM ... USING 'script'** | Hive streaming UDFs have no Trino equivalent. Rewrite as SQL or Python UDFs. |
| **SORT BY / DISTRIBUTE BY / CLUSTER BY** | MapReduce-era concepts. Use `ORDER BY` for global ordering and bucketed table properties for output partitioning. |
| **Hive variable substitution** | `${hiveconf:var}` must move to the client/orchestration layer. |
| **LOAD DATA INPATH** | No Trino equivalent. Rewrite as `INSERT INTO ... SELECT` from an external table. |
| **ACID/transactional tables** | Trino can read Hive ACID tables (Hive 3+ only) but has write restrictions. Compaction must still be handled by the Hive Compactor service. |
| **Hive views with complex features** | Views using non-literal array subscripts, nested GROUP BYs, or complex UDFs may produce incorrect results through the Coral translation layer. |
| **Partition metadata sync** | Trino INSERT on partitioned Hive tables may not sync metadata with the Hive Metastore. Add `CALL system.sync_partition_metadata()` after writes to partitioned tables. |
| **`CAST()` failure behavior** | Impala's `CAST()` returns NULL on conversion failure; Trino throws an error. Use `TRY_CAST()` in Trino for equivalent behavior. Requires manual review of each CAST to assess data quality risk. |
| **Timestamp/timezone semantics** | Impala has a single `TIMESTAMP` type with no timezone; Trino distinguishes `TIMESTAMP` from `TIMESTAMP WITH TIME ZONE`. The `FROM_UTC_TIMESTAMP` / `TO_UTC_TIMESTAMP` conversions are handled, but implicit timezone assumptions in existing data may still cause issues. |
| **Bucketing hash differences** | Hive v1 uses `hashCode()`, v2 uses Murmur3, and Spark-created bucketed tables may use a different hash entirely. Trino cannot write to Spark-bucketed tables. Timestamp column bucketing is unsupported in Trino. |

---

## Recommended Validation Process

1. **Run with `--dry-run` first** to see which rules fire and how many regions
   are affected per file.

2. **Review `conversion_review_report.txt`** — generated automatically in the
   script directory after every conversion run. This report lists every file
   with items needing manual attention, organized by category (TODO, REMOVED,
   INSERT OVERWRITE, DDL ADJUSTMENT) with line numbers.

3. **Resolve all TODO items** — these mark semi/anti join rewrites, unsupported
   system functions (`pid()`, `coordinator()`, `sleep()`), and other constructs
   that require manual rewriting.

4. **Decide on REMOVED items** — statements like `COMPUTE STATS` and
   `INVALIDATE METADATA` were dropped. Add `ANALYZE` or other replacements
   where needed.

5. **Rewrite DDL ADJUSTMENT items** — `LOCATION` and `TBLPROPERTIES` clauses
   were commented out and need Trino/Presto connector-specific syntax.
   `STORED AS` clauses are auto-converted to `WITH (format = '...')`.
   If the original DDL combined `STORED AS` with `LOCATION`, you may need
   to merge them into a single `WITH (format = '...', external_location = '...')`
   clause.

6. **Handle INSERT OVERWRITE items** — converted to `INSERT INTO`. Set the
   session property `insert_existing_partitions_behavior = 'OVERWRITE'` or
   add a `DELETE` / `TRUNCATE` before the `INSERT`.

7. **Review all ORDER BY clauses** for missing `NULLS FIRST` / `NULLS LAST`.

8. **Review all integer division** for potential truncation differences.

9. **Review all CAST() calls** and consider whether `TRY_CAST()` is needed
   for data-quality safety.

10. **Run the converted queries in parallel** against both the old and new
    engines, comparing row counts and sample output. Silent behavioral
    differences (NULL ordering, decimal precision, integer division) cannot be
    caught by syntax validation alone.
