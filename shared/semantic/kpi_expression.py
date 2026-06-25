"""KPI Expression DSL parser and validator (v2).

The KPI expression language is a safe, sandboxed DSL that compiles to SQL
via sqlglot. Expressions reference measures by name (``measure("Revenue")``),
KPIs by name (``kpi("Conversion Rate")``), and dimensions by name
(``dimension("Region")``).

The parser produces an AST with position tracking for inline error markers
in the formula editor. Validation runs at both parse time (structural) and
semantic time (logical consistency against the model).

See ``docs/architecture/architecture_kpi-requirements-specification.md``
sections 5.1-5.7 for the full language specification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Optional


# ---------------------------------------------------------------------------
# Function registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FunctionDef:
    """Definition of a DSL function."""
    name: str
    min_args: int
    max_args: int
    return_type: str = "numeric"
    category: str = "core"
    is_time_intelligence: bool = False


# All supported functions per spec Sections 5.2 and 5.3.
FUNCTION_REGISTRY: dict[str, FunctionDef] = {}

def _reg(name: str, min_args: int, max_args: int, **kw: object) -> None:
    FUNCTION_REGISTRY[name] = FunctionDef(name=name, min_args=min_args, max_args=max_args, **kw)  # type: ignore[arg-type]

# Core functions (Section 5.2)
_reg("measure",        1, 1, category="reference")
_reg("kpi",            1, 1, category="reference")
_reg("literal",        1, 1, category="reference")
_reg("dimension",      1, 1, category="reference")
_reg("safe_div",       2, 2, category="safe_division")
_reg("safe_ratio",     2, 2, category="safe_division")
_reg("div",            3, 3, category="safe_division")
_reg("coalesce",       2, 10, category="conditional")
_reg("if_then_else",   3, 3, category="conditional")
_reg("sla_condition",  5, 5, category="conditional")
_reg("abs",            1, 1, category="arithmetic")
_reg("round",          2, 2, category="arithmetic")
_reg("min_of",         2, 2, category="arithmetic")
_reg("max_of",         2, 2, category="arithmetic")

# Aggregation overrides (business builder)
_reg("sum",            1, 1, category="aggregation")
_reg("avg",            1, 1, category="aggregation")
_reg("min",            1, 1, category="aggregation")
_reg("max",            1, 1, category="aggregation")
_reg("count",          0, 1, category="aggregation")
_reg("count_distinct", 1, 1, category="aggregation")

# Windowed analytics (business builder)
_reg("share_of_total", 1, 1, category="analytics")
_reg("rank_over",      1, 1, category="analytics")

# Time intelligence functions (Section 5.3)
_reg("prior_period",          2, 2, category="period_comparison", is_time_intelligence=True)
_reg("period_to_date",        2, 2, category="accumulation",      is_time_intelligence=True)
_reg("moving_avg",            3, 3, category="accumulation",      is_time_intelligence=True)
_reg("trailing_sum",          3, 3, category="accumulation",      is_time_intelligence=True)
_reg("lag",                   3, 3, category="period_comparison", is_time_intelligence=True)
_reg("lead",                  3, 3, category="period_comparison", is_time_intelligence=True)
_reg("cagr",                  2, 2, category="growth",            is_time_intelligence=True)
_reg("pct_change",            2, 2, category="period_comparison", is_time_intelligence=True)
_reg("fiscal_period_to_date", 2, 2, category="accumulation",      is_time_intelligence=True)

VALID_GRAINS = frozenset({"day", "week", "month", "quarter", "year"})


# ---------------------------------------------------------------------------
# Token types
# ---------------------------------------------------------------------------

class TokenType:
    NUMBER    = "NUMBER"
    STRING    = "STRING"
    IDENT     = "IDENT"
    LPAREN    = "LPAREN"
    RPAREN    = "RPAREN"
    COMMA     = "COMMA"
    PLUS      = "PLUS"
    MINUS     = "MINUS"
    STAR      = "STAR"
    SLASH     = "SLASH"
    EOF       = "EOF"


@dataclass
class Token:
    type: str
    value: str
    pos: int  # character offset in source


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

_TOKEN_PATTERNS = [
    (TokenType.NUMBER,  r'\d+(?:\.\d+)?'),
    (TokenType.STRING,  r'"[^"]*"|\'[^\']*\''),
    (TokenType.IDENT,   r'[a-zA-Z_][a-zA-Z0-9_]*'),
    (TokenType.LPAREN,  r'\('),
    (TokenType.RPAREN,  r'\)'),
    (TokenType.COMMA,   r','),
    (TokenType.PLUS,    r'\+'),
    (TokenType.MINUS,   r'-'),
    (TokenType.STAR,    r'\*'),
    (TokenType.SLASH,   r'/'),
]
_TOKEN_RE = re.compile(
    '|'.join(f'(?P<{name}>{pattern})' for name, pattern in _TOKEN_PATTERNS)
)
_WHITESPACE_RE = re.compile(r'\s+')


def tokenize(expression: str) -> list[Token]:
    """Tokenize a KPI expression string into a list of tokens."""
    tokens: list[Token] = []
    pos = 0
    while pos < len(expression):
        ws = _WHITESPACE_RE.match(expression, pos)
        if ws:
            pos = ws.end()
            continue
        m = _TOKEN_RE.match(expression, pos)
        if not m:
            raise KPIExpressionError(
                f"Unexpected character {expression[pos]!r} at position {pos}",
                code="SYNTAX_ERROR",
                position={"start": pos, "end": pos + 1},
            )
        token_type = m.lastgroup
        assert token_type is not None
        tokens.append(Token(type=token_type, value=m.group(), pos=pos))
        pos = m.end()
    tokens.append(Token(type=TokenType.EOF, value="", pos=pos))
    return tokens


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------

@dataclass
class ASTNode:
    pos_start: int = 0
    pos_end: int = 0


@dataclass
class NumberLiteral(ASTNode):
    value: float = 0.0


@dataclass
class StringLiteral(ASTNode):
    value: str = ""


@dataclass
class FunctionCall(ASTNode):
    name: str = ""
    args: list[ASTNode] = field(default_factory=list)


@dataclass
class BinaryOp(ASTNode):
    op: str = ""
    left: ASTNode = field(default_factory=ASTNode)
    right: ASTNode = field(default_factory=ASTNode)


@dataclass
class UnaryMinus(ASTNode):
    operand: ASTNode = field(default_factory=ASTNode)


# ---------------------------------------------------------------------------
# Recursive-descent parser
# ---------------------------------------------------------------------------

class KPIExpressionError(ValueError):
    """Raised when a KPI expression fails parse or validation."""
    def __init__(
        self,
        message: str,
        *,
        code: str = "UNKNOWN",
        position: Optional[dict] = None,
        suggestion: Optional[str] = None,
    ):
        super().__init__(message)
        self.code = code
        self.position = position
        self.suggestion = suggestion


class Parser:
    """Recursive-descent parser for the KPI expression DSL."""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def _current(self) -> Token:
        return self.tokens[self.pos]

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def _expect(self, token_type: str) -> Token:
        tok = self._current()
        if tok.type != token_type:
            raise KPIExpressionError(
                f"Expected {token_type} at position {tok.pos}; got {tok.type} ({tok.value!r})",
                code="SYNTAX_ERROR",
                position={"start": tok.pos, "end": tok.pos + len(tok.value)},
            )
        return self._advance()

    def parse(self) -> ASTNode:
        node = self._parse_additive()
        if self._current().type != TokenType.EOF:
            tok = self._current()
            raise KPIExpressionError(
                f"Unexpected token {tok.value!r} at position {tok.pos}; expected end of expression",
                code="SYNTAX_ERROR",
                position={"start": tok.pos, "end": tok.pos + len(tok.value)},
            )
        return node

    def _parse_additive(self) -> ASTNode:
        left = self._parse_multiplicative()
        while self._current().type in (TokenType.PLUS, TokenType.MINUS):
            op_tok = self._advance()
            right = self._parse_multiplicative()
            left = BinaryOp(
                op=op_tok.value,
                left=left,
                right=right,
                pos_start=left.pos_start,
                pos_end=right.pos_end,
            )
        return left

    def _parse_multiplicative(self) -> ASTNode:
        left = self._parse_unary()
        while self._current().type in (TokenType.STAR, TokenType.SLASH):
            op_tok = self._advance()
            if op_tok.type == TokenType.SLASH:
                raise KPIExpressionError(
                    "Direct division is not allowed in KPI expressions because "
                    "it can produce division-by-zero errors. Use safe_div(a, b) "
                    "to return NULL on zero, or div(a, b, fallback) to return a "
                    "custom value.",
                    code="BARE_DIVISION",
                    position={"start": op_tok.pos, "end": op_tok.pos + 1},
                    suggestion="Wrap in safe_div(a, b)",
                )
            right = self._parse_unary()
            left = BinaryOp(
                op=op_tok.value,
                left=left,
                right=right,
                pos_start=left.pos_start,
                pos_end=right.pos_end,
            )
        return left

    def _parse_unary(self) -> ASTNode:
        if self._current().type == TokenType.MINUS:
            op_tok = self._advance()
            operand = self._parse_primary()
            return UnaryMinus(
                operand=operand,
                pos_start=op_tok.pos,
                pos_end=operand.pos_end,
            )
        return self._parse_primary()

    def _parse_primary(self) -> ASTNode:
        tok = self._current()

        if tok.type == TokenType.NUMBER:
            self._advance()
            return NumberLiteral(
                value=float(tok.value),
                pos_start=tok.pos,
                pos_end=tok.pos + len(tok.value),
            )

        if tok.type == TokenType.STRING:
            self._advance()
            return StringLiteral(
                value=tok.value[1:-1],  # strip quotes
                pos_start=tok.pos,
                pos_end=tok.pos + len(tok.value),
            )

        if tok.type == TokenType.IDENT:
            # Check if it's a function call
            if self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].type == TokenType.LPAREN:
                return self._parse_function_call()
            # Bare identifier — not allowed
            self._advance()
            raise KPIExpressionError(
                f"Bare identifier {tok.value!r} is not allowed. Use "
                f'measure("{tok.value}") to reference a measure, or '
                f'kpi("{tok.value}") to reference a KPI.',
                code="BARE_IDENTIFIER",
                position={"start": tok.pos, "end": tok.pos + len(tok.value)},
                suggestion=f'measure("{tok.value}")',
            )

        if tok.type == TokenType.LPAREN:
            self._advance()
            node = self._parse_additive()
            close = self._expect(TokenType.RPAREN)
            node.pos_start = tok.pos
            node.pos_end = close.pos + 1
            return node

        raise KPIExpressionError(
            f"Unexpected token {tok.value!r} at position {tok.pos}; expected "
            "expression",
            code="SYNTAX_ERROR",
            position={"start": tok.pos, "end": tok.pos + len(tok.value)},
        )

    def _parse_function_call(self) -> ASTNode:
        name_tok = self._advance()
        fn_name = name_tok.value.lower()
        self._expect(TokenType.LPAREN)

        args: list[ASTNode] = []
        if self._current().type != TokenType.RPAREN:
            args.append(self._parse_additive())
            while self._current().type == TokenType.COMMA:
                self._advance()
                args.append(self._parse_additive())

        close = self._expect(TokenType.RPAREN)
        return FunctionCall(
            name=fn_name,
            args=args,
            pos_start=name_tok.pos,
            pos_end=close.pos + 1,
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class Diagnostic:
    code: str
    message: str
    position: Optional[dict] = None
    suggestion: Optional[str] = None
    severity: str = "error"  # "error" or "warning"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "position": self.position,
            "suggestion": self.suggestion,
        }


@dataclass
class ValidationResult:
    valid: bool
    errors: list[Diagnostic] = field(default_factory=list)
    warnings: list[Diagnostic] = field(default_factory=list)
    referenced_measures: list[str] = field(default_factory=list)
    referenced_kpis: list[str] = field(default_factory=list)
    referenced_dimensions: list[str] = field(default_factory=list)
    has_time_intelligence: bool = False
    requires_time_dimension: bool = False
    detected_agg_mode: Optional[str] = None
    expression_tree: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "referenced_measures": self.referenced_measures,
            "referenced_kpis": self.referenced_kpis,
            "referenced_dimensions": self.referenced_dimensions,
            "has_time_intelligence": self.has_time_intelligence,
            "requires_time_dimension": self.requires_time_dimension,
            "detected_agg_mode": self.detected_agg_mode,
            "expression_tree": self.expression_tree,
        }


def _ast_to_dict(node: ASTNode) -> dict:
    """Serialize an AST node to a JSON-compatible dict."""
    if isinstance(node, NumberLiteral):
        return {"type": "number", "value": node.value}
    if isinstance(node, StringLiteral):
        return {"type": "string", "value": node.value}
    if isinstance(node, FunctionCall):
        return {"type": "call", "fn": node.name, "args": [_ast_to_dict(a) for a in node.args]}
    if isinstance(node, BinaryOp):
        return {"type": "binary", "op": node.op, "left": _ast_to_dict(node.left), "right": _ast_to_dict(node.right)}
    if isinstance(node, UnaryMinus):
        return {"type": "unary_minus", "operand": _ast_to_dict(node.operand)}
    return {"type": "unknown"}


def _collect_references(node: ASTNode) -> tuple[list[str], list[str], list[str]]:
    """Walk the AST and collect measure, kpi, and dimension references."""
    measures: list[str] = []
    kpis: list[str] = []
    dimensions: list[str] = []

    def _walk(n: ASTNode) -> None:
        if isinstance(n, FunctionCall):
            if n.name == "measure" and n.args and isinstance(n.args[0], StringLiteral):
                measures.append(n.args[0].value)
            elif n.name == "kpi" and n.args and isinstance(n.args[0], StringLiteral):
                kpis.append(n.args[0].value)
            elif n.name == "dimension" and n.args and isinstance(n.args[0], StringLiteral):
                dimensions.append(n.args[0].value)
            for arg in n.args:
                _walk(arg)
        elif isinstance(n, BinaryOp):
            _walk(n.left)
            _walk(n.right)
        elif isinstance(n, UnaryMinus):
            _walk(n.operand)

    _walk(node)
    return measures, kpis, dimensions


def _check_time_intelligence(node: ASTNode) -> bool:
    """Return True if the AST contains any time-intelligence function call."""
    if isinstance(node, FunctionCall):
        fn_def = FUNCTION_REGISTRY.get(node.name)
        if fn_def and fn_def.is_time_intelligence:
            return True
        return any(_check_time_intelligence(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return _check_time_intelligence(node.left) or _check_time_intelligence(node.right)
    if isinstance(node, UnaryMinus):
        return _check_time_intelligence(node.operand)
    return False


def _has_kpi_ref(node: ASTNode) -> bool:
    """Return True if the AST subtree contains a kpi() reference."""
    if isinstance(node, FunctionCall):
        if node.name == "kpi":
            return True
        return any(_has_kpi_ref(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return _has_kpi_ref(node.left) or _has_kpi_ref(node.right)
    if isinstance(node, UnaryMinus):
        return _has_kpi_ref(node.operand)
    return False


def _check_kpi_inside_time_function(node: ASTNode) -> bool:
    """Return True if any time-intelligence function wraps a kpi() reference.

    This combination cannot be compiled to SQL and the Python fallback
    does not support time-shifted evaluation, so it will return no data.
    """
    if isinstance(node, FunctionCall):
        fn_def = FUNCTION_REGISTRY.get(node.name)
        if fn_def and fn_def.is_time_intelligence:
            # Check if any argument subtree contains a kpi() ref
            return any(_has_kpi_ref(a) for a in node.args)
        # Recurse into non-time function arguments
        return any(_check_kpi_inside_time_function(a) for a in node.args)
    if isinstance(node, BinaryOp):
        return _check_kpi_inside_time_function(node.left) or _check_kpi_inside_time_function(node.right)
    if isinstance(node, UnaryMinus):
        return _check_kpi_inside_time_function(node.operand)
    return False


def _detect_agg_mode(
    ast: ASTNode,
    referenced_kpis: list[str],
    referenced_measures: list[str],
) -> str:
    """Detect the aggregation mode from the expression structure (Section 5.4.6)."""
    # R1: Only kpi() references (no measure()) -> pre_aggregated
    if referenced_kpis and not referenced_measures:
        return "pre_aggregated"
    # R2: Ratio of two measures via safe_div/safe_ratio -> aggregate_first
    if isinstance(ast, FunctionCall) and ast.name in ("safe_div", "safe_ratio"):
        return "aggregate_first"
    # R5: Fallback
    return "aggregate_first"


def validate_expression(
    expression: str,
    *,
    model_measures: Optional[set[str]] = None,
    model_kpis: Optional[set[str]] = None,
    model_dimensions: Optional[set[str]] = None,
    has_time_dimension: bool = True,
) -> ValidationResult:
    """Parse and validate a KPI expression, returning a full diagnostic result.

    This is the main entry point for expression validation. It performs:
    1. Tokenization and parsing (structural validation)
    2. Function registry checks (unknown functions, arg counts)
    3. Reference validation (measure/kpi/dimension names)
    4. Semantic validation (time intelligence requirements, grain values)
    5. Aggregation mode detection

    Parameters
    ----------
    expression : str
        The raw expression string to validate.
    model_measures : set[str] | None
        Set of valid measure names in the model. If None, name checks are skipped.
    model_kpis : set[str] | None
        Set of valid KPI names in the model. If None, name checks are skipped.
    model_dimensions : set[str] | None
        Set of valid dimension names in the model. If None, name checks are skipped.
    has_time_dimension : bool
        Whether the KPI or its measures have a time dimension binding.
    """
    result = ValidationResult(valid=True)

    if not expression or not expression.strip():
        result.valid = False
        result.errors.append(Diagnostic(
            code="EMPTY_EXPRESSION",
            message="Expression is empty",
        ))
        return result

    # 1. Tokenize
    try:
        tokens = tokenize(expression)
    except KPIExpressionError as exc:
        result.valid = False
        result.errors.append(Diagnostic(
            code=exc.code,
            message=str(exc),
            position=exc.position,
            suggestion=exc.suggestion,
        ))
        return result

    # 2. Parse
    try:
        ast = Parser(tokens).parse()
    except KPIExpressionError as exc:
        result.valid = False
        result.errors.append(Diagnostic(
            code=exc.code,
            message=str(exc),
            position=exc.position,
            suggestion=exc.suggestion,
        ))
        return result

    result.expression_tree = _ast_to_dict(ast)

    # 3. Validate function calls
    _validate_functions(ast, result)

    # 4. Collect references
    measures, kpis, dimensions = _collect_references(ast)
    result.referenced_measures = list(dict.fromkeys(measures))  # dedupe, preserve order
    result.referenced_kpis = list(dict.fromkeys(kpis))
    result.referenced_dimensions = list(dict.fromkeys(dimensions))

    # 5. Validate references against model
    if model_measures is not None:
        for name in result.referenced_measures:
            if name not in model_measures:
                similar = get_close_matches(name, model_measures, n=1, cutoff=0.6)
                suggestion = f'Did you mean measure("{similar[0]}")?' if similar else None
                result.valid = False
                result.errors.append(Diagnostic(
                    code="UNKNOWN_MEASURE",
                    message=f'Measure "{name}" not found in model. '
                            f"Available measures: {sorted(model_measures)[:5]}",
                    suggestion=suggestion,
                ))

    if model_kpis is not None:
        for name in result.referenced_kpis:
            if name not in model_kpis:
                similar = get_close_matches(name, model_kpis, n=1, cutoff=0.6)
                suggestion = f'Did you mean kpi("{similar[0]}")?' if similar else None
                result.valid = False
                result.errors.append(Diagnostic(
                    code="UNKNOWN_KPI",
                    message=f'KPI "{name}" not found in model.',
                    suggestion=suggestion,
                ))

    if model_dimensions is not None:
        for name in result.referenced_dimensions:
            if name not in model_dimensions:
                result.valid = False
                result.errors.append(Diagnostic(
                    code="UNKNOWN_DIMENSION",
                    message=f'Dimension "{name}" not found in model.',
                ))

    # 6. Time intelligence checks
    result.has_time_intelligence = _check_time_intelligence(ast)
    result.requires_time_dimension = result.has_time_intelligence
    if result.requires_time_dimension and not has_time_dimension:
        result.valid = False
        result.errors.append(Diagnostic(
            code="TIME_DIMENSION_REQUIRED",
            message="This expression uses time intelligence functions that require "
                    "a time dimension binding on this KPI or its measures.",
        ))

    # 6b. Reject kpi() refs inside time functions — this combination
    # cannot be evaluated (SQL compiler rejects it, Python fallback returns None).
    if result.has_time_intelligence and result.referenced_kpis:
        if _check_kpi_inside_time_function(ast):
            result.valid = False
            result.errors.append(Diagnostic(
                code="KPI_INSIDE_TIME_FUNCTION",
                message="A kpi() reference inside a time intelligence function "
                        "cannot be evaluated. Time functions require direct "
                        "measure references to compute period-shifted values. "
                        "Consider referencing the underlying measure directly.",
                suggestion="Replace kpi(...) with measure(...) inside time functions.",
            ))

    # 7. Detect aggregation mode
    result.detected_agg_mode = _detect_agg_mode(ast, result.referenced_kpis, result.referenced_measures)

    return result


def _validate_functions(node: ASTNode, result: ValidationResult) -> None:
    """Recursively validate all function calls in the AST."""
    if isinstance(node, FunctionCall):
        fn_def = FUNCTION_REGISTRY.get(node.name)
        if fn_def is None:
            all_names = list(FUNCTION_REGISTRY.keys())
            similar = get_close_matches(node.name, all_names, n=1, cutoff=0.6)
            suggestion = f'Did you mean "{similar[0]}"?' if similar else None
            result.valid = False
            result.errors.append(Diagnostic(
                code="UNKNOWN_FUNCTION",
                message=f'Function "{node.name}" is not recognised.',
                position={"start": node.pos_start, "end": node.pos_end},
                suggestion=suggestion,
            ))
        else:
            arg_count = len(node.args)
            if arg_count < fn_def.min_args or arg_count > fn_def.max_args:
                if fn_def.min_args == fn_def.max_args:
                    expected = f"{fn_def.min_args}"
                else:
                    expected = f"{fn_def.min_args}-{fn_def.max_args}"
                result.valid = False
                result.errors.append(Diagnostic(
                    code="ARGUMENT_COUNT",
                    message=f'Function {node.name} expects {expected} argument(s); got {arg_count}.',
                    position={"start": node.pos_start, "end": node.pos_end},
                ))

            # Validate grain arguments for time-intelligence functions
            if fn_def.is_time_intelligence and node.args:
                _validate_grain_args(node, fn_def, result)

            # Validate reference function args have string arguments
            if node.name in ("measure", "kpi", "dimension"):
                if node.args and not isinstance(node.args[0], StringLiteral):
                    result.valid = False
                    result.errors.append(Diagnostic(
                        code="TYPE_MISMATCH",
                        message=f'Function {node.name} expects a quoted string argument; '
                                f'use {node.name}("name")',
                        position={"start": node.pos_start, "end": node.pos_end},
                    ))

        # Recurse into arguments
        for arg in node.args:
            _validate_functions(arg, result)

    elif isinstance(node, BinaryOp):
        _validate_functions(node.left, result)
        _validate_functions(node.right, result)
    elif isinstance(node, UnaryMinus):
        _validate_functions(node.operand, result)


def _validate_grain_args(
    node: FunctionCall,
    fn_def: FunctionDef,
    result: ValidationResult,
) -> None:
    """Validate grain/period arguments for time-intelligence functions."""
    # Functions with grain as last argument:
    # prior_period(expr, period), pct_change(expr, period)
    # moving_avg(expr, n, grain), trailing_sum(expr, n, grain)
    # lag(expr, n, grain), lead(expr, n, grain)
    # period_to_date(expr, period), fiscal_period_to_date(expr, period)
    # cagr has no grain argument

    if node.name == "cagr":
        return

    # F-017-15: the grain is a StringLiteral but its POSITION varies — the spec
    # documents (expr, n, grain) while the wizard/builder emit
    # (expr, grain, literal(n)). Validating only the last argument silently
    # skipped grain validation for builder-order expressions (last arg is the
    # literal(n) FunctionCall). Find the first StringLiteral after the
    # expression and validate it positionally, so an invalid grain is caught
    # under either argument order.
    grain_arg = next(
        (a for a in node.args[1:] if isinstance(a, StringLiteral)), None,
    )
    if grain_arg is not None and grain_arg.value not in VALID_GRAINS:
        result.valid = False
        result.errors.append(Diagnostic(
            code="INVALID_GRAIN",
            message=f'Grain "{grain_arg.value}" is not supported. '
                    f"Valid values: {', '.join(sorted(VALID_GRAINS))}",
            position={"start": grain_arg.pos_start, "end": grain_arg.pos_end},
        ))


# ---------------------------------------------------------------------------
# Convenience: parse-only (raises on first error)
# ---------------------------------------------------------------------------

def parse_kpi_expression(expression: str) -> ASTNode:
    """Parse a KPI expression and return the AST. Raises on error."""
    if not expression or not expression.strip():
        raise KPIExpressionError("Expression is empty", code="EMPTY_EXPRESSION")
    tokens = tokenize(expression)
    return Parser(tokens).parse()


def extract_measure_names(expression: str) -> list[str]:
    """Return deduplicated measure names referenced by *expression*.

    Returns an empty list if the expression is empty or unparseable.
    """
    if not expression or not expression.strip():
        return []
    try:
        ast = parse_kpi_expression(expression)
        measures, _, _ = _collect_references(ast)
        return list(dict.fromkeys(measures))
    except Exception:
        return []
