/**
 * Tree-sitter grammar for DAX (Data Analysis Expressions).
 *
 * Covers the subset used by Excel DirectQuery and Power BI:
 *   EVALUATE, SUMMARIZECOLUMNS, SUMMARIZE, CALCULATE, CALCULATETABLE,
 *   FILTER, ROW, TOPN, SELECTCOLUMNS, ADDCOLUMNS, DISTINCT, VALUES,
 *   VAR/RETURN, aggregate functions, time-intelligence stubs,
 *   DIVIDE, IF, arithmetic, and comparison operators.
 */
module.exports = grammar({
  name: "dax",

  extras: $ => [/\s+/],

  word: $ => $.identifier,

  conflicts: $ => [],

  rules: {
    source_file: $ => $.dax_statement,

    dax_statement: $ => seq(
      caseInsensitive("EVALUATE"),
      $.expression,
    ),

    expression: $ => choice(
      $.var_block,
      $.summarize_columns,
      $.summarize,
      $.calculate,
      $.calculate_table,
      $.filter_func,
      $.row_func,
      $.topn,
      $.select_columns,
      $.add_columns,
      $.distinct_func,
      $.values_func,
      $.aggregate_func,
      $.time_intel_func,
      $.divide_func,
      $.if_func,
      $.treatas_func,
      $.all_func,
      $.allselected_func,
      $.removefilters_func,
      $.binary_expr,
      $.paren_expr,
      $.column_ref,
      $.bare_identifier,
      $.number,
      $.string_literal,
    ),

    // Plain identifier — variable reference or table name in non-bracketed context
    bare_identifier: $ => prec(-1, $.identifier),

    // VAR x = expr [VAR y = expr] RETURN expr
    var_block: $ => seq(
      repeat1($.var_decl),
      caseInsensitive("RETURN"),
      field("body", $.expression),
    ),

    var_decl: $ => seq(
      caseInsensitive("VAR"),
      field("name", $.identifier),
      "=",
      field("value", $.expression),
    ),

    // SUMMARIZECOLUMNS(col, col, ..., filter, ..., "label", expr, ...)
    summarize_columns: $ => seq(
      caseInsensitive("SUMMARIZECOLUMNS"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // SUMMARIZE(table, col, ..., "label", expr, ...)
    summarize: $ => seq(
      caseInsensitive("SUMMARIZE"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // CALCULATE(measure, filter1, filter2, ...)
    calculate: $ => prec(2, seq(
      caseInsensitive("CALCULATE"),
      "(",
      commaSep1($.argument),
      ")",
    )),

    // CALCULATETABLE(table_expr, filter1, ...)
    calculate_table: $ => seq(
      caseInsensitive("CALCULATETABLE"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // FILTER(table, condition)
    filter_func: $ => seq(
      caseInsensitive("FILTER"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // ROW("label", expression)
    row_func: $ => seq(
      caseInsensitive("ROW"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // TOPN(n, table_expr, order_col, direction)
    topn: $ => seq(
      caseInsensitive("TOPN"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // SELECTCOLUMNS(table, "Name", expr, ...)
    select_columns: $ => seq(
      caseInsensitive("SELECTCOLUMNS"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // ADDCOLUMNS(table, "Name", expr, ...)
    add_columns: $ => seq(
      caseInsensitive("ADDCOLUMNS"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // DISTINCT(column) / VALUES(column)
    distinct_func: $ => seq(
      caseInsensitive("DISTINCT"),
      "(",
      $.argument,
      ")",
    ),

    values_func: $ => seq(
      caseInsensitive("VALUES"),
      "(",
      $.argument,
      ")",
    ),

    // SUM, AVERAGE, COUNT, COUNTROWS, MIN, MAX, DISTINCTCOUNT
    aggregate_func: $ => seq(
      field("func_name", $.agg_keyword),
      "(",
      commaSep($.argument),
      ")",
    ),

    agg_keyword: $ => choice(
      caseInsensitive("SUM"),
      caseInsensitive("AVERAGE"),
      caseInsensitive("COUNT"),
      caseInsensitive("COUNTROWS"),
      caseInsensitive("MIN"),
      caseInsensitive("MAX"),
      caseInsensitive("DISTINCTCOUNT"),
    ),

    // Time intelligence: TOTALYTD, TOTALQTD, TOTALMTD, etc.
    time_intel_func: $ => seq(
      field("func_name", $.time_intel_keyword),
      "(",
      commaSep1($.argument),
      ")",
    ),

    time_intel_keyword: $ => choice(
      caseInsensitive("TOTALYTD"),
      caseInsensitive("TOTALQTD"),
      caseInsensitive("TOTALMTD"),
      caseInsensitive("SAMEPERIODLASTYEAR"),
      caseInsensitive("PREVIOUSMONTH"),
      caseInsensitive("PREVIOUSQUARTER"),
      caseInsensitive("PREVIOUSYEAR"),
      caseInsensitive("DATEADD"),
    ),

    // DIVIDE(num, denom [, alt])
    divide_func: $ => seq(
      caseInsensitive("DIVIDE"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // IF(condition, true_val, false_val)
    if_func: $ => seq(
      caseInsensitive("IF"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // TREATAS(values, target_column)
    treatas_func: $ => seq(
      caseInsensitive("TREATAS"),
      "(",
      commaSep1($.argument),
      ")",
    ),

    // ALL(table | column)
    all_func: $ => seq(
      caseInsensitive("ALL"),
      "(",
      commaSep($.argument),
      ")",
    ),

    // ALLSELECTED(table | column)
    allselected_func: $ => seq(
      caseInsensitive("ALLSELECTED"),
      "(",
      commaSep($.argument),
      ")",
    ),

    // REMOVEFILTERS()
    removefilters_func: $ => seq(
      caseInsensitive("REMOVEFILTERS"),
      "(",
      commaSep($.argument),
      ")",
    ),

    // Binary expressions: arithmetic and comparison
    binary_expr: $ => choice(
      prec.left(1, seq($.expression, choice("=", "<>"), $.expression)),
      prec.left(1, seq($.expression, choice("<", ">", "<=", ">="), $.expression)),
      prec.left(2, seq($.expression, choice("+", "-"), $.expression)),
      prec.left(3, seq($.expression, choice("*", "/"), $.expression)),
    ),

    paren_expr: $ => seq("(", $.expression, ")"),

    // Generic argument — used inside function call argument lists
    argument: $ => choice(
      $.expression,
      $.set_literal,
      $.direction_keyword,
    ),

    // {value1, value2, ...} — set literal (used in TREATAS, etc.)
    set_literal: $ => seq(
      "{",
      commaSep(choice($.expression, $.set_literal)),
      "}",
    ),

    direction_keyword: $ => choice(
      caseInsensitive("ASC"),
      caseInsensitive("DESC"),
      caseInsensitive("YEAR"),
      caseInsensitive("QUARTER"),
      caseInsensitive("MONTH"),
      caseInsensitive("DAY"),
    ),

    // Table[Column] or 'Table Name'[Column] or [Column]
    column_ref: $ => choice(
      seq($.table_ref, $.bracket_name),
      $.bracket_name,
    ),

    // Table reference: identifier or quoted name
    table_ref: $ => choice(
      $.identifier,
      $.quoted_table_name,
    ),

    bracket_name: $ => /\[[^\]]+\]/,

    quoted_table_name: $ => /'[^']+'/,

    identifier: $ => /[A-Za-z_]\w*/,

    number: $ => /\d+(\.\d+)?/,

    string_literal: $ => /"[^"]*"/,
  },
});

// Helper: case-insensitive keyword
function caseInsensitive(word) {
  return new RegExp(
    word.split("").map(c => {
      if (/[a-zA-Z]/.test(c)) {
        return `[${c.toLowerCase()}${c.toUpperCase()}]`;
      }
      return c;
    }).join("")
  );
}

// Helper: comma-separated list (0+)
function commaSep(rule) {
  return optional(commaSep1(rule));
}

// Helper: comma-separated list (1+)
function commaSep1(rule) {
  return seq(rule, repeat(seq(",", rule)));
}
