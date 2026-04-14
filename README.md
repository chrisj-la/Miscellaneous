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
   `--!hive`, or `--!null` label (typically inside a SQL comment or JS
   comment). This label determines which conversion ruleset to apply and
   what the output label should be.

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

4. **Label replacement** — Swaps the dialect label to reflect the target
   dialect (e.g. `--!impala` → `--!trino`).

### Dialect routing

| Source Label | Target Label | Action |
|---|---|---|
| `--!impala` | `--!trino` | Impala SQL → Trino SQL |
| `--!hive` | `--!presto` | Hive SQL → Presto SQL |
| `--!null` | *(unchanged)* | File skipped entirely |
| `--!trino` / `--!presto` | *(unchanged)* | Already converted, skipped |
| *(no label)* | — | Warning logged, file skipped |

### File type handling

| Extension | Mode | How SQL is found |
|---|---|---|
| `.js` | Embedded SQL | SQL inside JS string literals (`"..."` / `'...'`) |
| `.sql`, `.hql`, `.hive`, `.ddl`, `.dml` | Raw SQL | Entire file content (minus protected blocks) |

---

## Usage

### Single file
```bash
python sql_dialect_converter.py input.sql -o output.sql
python sql_dialect_converter.py script.js -o converted.js
```

### Directory (all supported extensions)
```bash
python sql_dialect_converter.py input_dir/ -o output_dir/
python sql_dialect_converter.py input_dir/ -o output_dir/ --recursive
```

### Preview changes without writing
```bash
python sql_dialect_converter.py input.sql --dry-run
```

### Run built-in self-test
```bash
python sql_dialect_converter.py --self-test
```

### Full option list
```
python sql_dialect_converter.py [-h] [-o OUTPUT] [-r] [-n] [-v] [--self-test] [input]

positional arguments:
  input                 Input .js/.sql file or directory

options:
  -o, --output          Output file or directory
  -r, --recursive       Recursively process directories
  -n, --dry-run         Preview changes without writing
  -v, --verbose         Enable debug-level logging
  --self-test           Run built-in self-test (6 test cases)
```

---

## Conversions Performed

### Impala → Trino (53 active rules + DECODE pre-pass)

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

**DDL / storage clauses (commented out with adjustment note):**

`STORED AS PARQUET/ORC/...`, `ROW FORMAT DELIMITED`, `LOCATION '...'`,
`TBLPROPERTIES(...)`, `SORT BY(...)`

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
| `UNIX_TIMESTAMP()` | `TO_UNIXTIME(NOW())` |
| `UNIX_TIMESTAMP(expr)` | `TO_UNIXTIME(expr)` |
| `FROM_TIMESTAMP(ts, fmt)` | `DATE_FORMAT(ts, fmt)` |
| `CURRENT_TIMESTAMP()` | `NOW()` |
| `FROM_UTC_TIMESTAMP(ts, tz)` | `CAST(ts AS TIMESTAMP) AT TIME ZONE tz` |
| `TO_UTC_TIMESTAMP(ts, tz)` | `CAST(ts AT TIME ZONE tz ...) AT TIME ZONE 'UTC'` |

**Functions — string:**

| Impala | Trino |
|---|---|
| `STRLEFT(s, n)` | `SUBSTR(s, 1, n)` |
| `STRRIGHT(s, n)` | `SUBSTR(s, -n)` |
| `INSTR(hay, needle)` | `STRPOS(hay, needle)` |
| `GROUP_CONCAT(col)` | `ARRAY_JOIN(ARRAY_AGG(col), ',')` |

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

**Identifiers / misc:**

| Impala | Trino |
|---|---|
| `` `column_name` `` | `"column_name"` |
| `/* +SHUFFLE */` hints | Removed |
| `SHOW DATABASES` | `SHOW SCHEMAS` |
| `TABLESAMPLE SYSTEM(n)` | `TABLESAMPLE BERNOULLI(n)` |

---

### Hive → Presto (48 active rules + DECODE pre-pass)

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

**Aggregate:**

| Hive | Presto |
|---|---|
| `PERCENTILE_APPROX(col, pct)` | `APPROX_PERCENTILE(col, pct)` |

**LATERAL VIEW → CROSS JOIN UNNEST:**

| Hive | Presto |
|---|---|
| `LATERAL VIEW EXPLODE(col) t AS c` | `CROSS JOIN UNNEST(col) AS t(c)` |
| `LATERAL VIEW POSEXPLODE(col) t AS p, c` | `CROSS JOIN UNNEST(col) WITH ORDINALITY AS t(c, p)` |

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

### Conversions that require manual review

| Issue | What the script does | What you need to do |
|---|---|---|
| **LEFT SEMI JOIN** | Replaces with `JOIN` + inserts a `/* TODO */` comment | Rewrite to `WHERE EXISTS (SELECT 1 FROM t2 WHERE ...)` |
| **LEFT ANTI JOIN** | Replaces with `LEFT JOIN` + inserts a `/* TODO */` comment | Rewrite to `WHERE NOT EXISTS (...)` or add `WHERE t2.key IS NULL` |
| **RIGHT SEMI / ANTI JOIN** | Same pattern | Same approach, reversed |
| **INSERT OVERWRITE** | Replaces with `INSERT INTO` + comment | Set session property `insert_existing_partitions_behavior = 'OVERWRITE'` or add a `DELETE` / `TRUNCATE` before the `INSERT` |
| **STORED AS / LOCATION / TBLPROPERTIES** | Comments out with adjustment note | Rewrite using Trino's `WITH (format = '...', external_location = '...')` syntax |
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

2. **Search the output for `TODO`** — these mark semi/anti join rewrites that
   must be completed manually.

3. **Search the output for `REMOVED`** — these mark statements like
   `COMPUTE STATS` and `INVALIDATE METADATA` that were dropped. Decide if
   replacements are needed.

4. **Search the output for `adjust for`** — these mark DDL clauses like
   `STORED AS PARQUET` that were commented out and need Trino/Presto
   connector-specific syntax.

5. **Review all ORDER BY clauses** for missing `NULLS FIRST` / `NULLS LAST`.

6. **Review all integer division** for potential truncation differences.

7. **Review all CAST() calls** and consider whether `TRY_CAST()` is needed
   for data-quality safety.

8. **Run the converted queries in parallel** against both the old and new
   engines, comparing row counts and sample output. Silent behavioral
   differences (NULL ordering, decimal precision, integer division) cannot be
   caught by syntax validation alone.
