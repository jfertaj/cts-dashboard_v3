/**
 * filterLogic.ts
 *
 * Logic expression parser and converters for the Salesforce-style filter system.
 *
 * Supports expressions like:
 *   "1 AND 2"
 *   "1 OR 2 OR 3"
 *   "(1 AND 2) OR 3"
 *   "1 AND (2 OR 3)"
 *   "(1 AND 2) OR (3 AND 4)"
 *   "((1 OR 2) AND 3) OR (4 AND 5)"
 */

import type { FilterGroup } from "./api";

// ---- Types ----

export type FlatRule = {
  id: number;       // 1-indexed, stable across session
  field: string;
  operator: string;
  value: any;
};

export type FlatFilterSet = {
  rules: FlatRule[];
  logicExpr: string;  // e.g. "1 AND (2 OR 3)"
};

// ---- Internal AST ----

type ASTNum = { type: "NUM"; value: number };
type ASTBinary = { type: "AND" | "OR"; children: ASTNode[] };
type ASTNode = ASTNum | ASTBinary;

// ---- Tokenizer ----

type TokenType = "NUM" | "AND" | "OR" | "LPAREN" | "RPAREN" | "EOF";
type Token = { type: TokenType; value?: number };

export function tokenize(expr: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  const s = expr.trim();
  while (i < s.length) {
    const ch = s[i];
    if (ch === " " || ch === "\t" || ch === "\n" || ch === "\r") { i++; continue; }
    if (ch === "(") { tokens.push({ type: "LPAREN" }); i++; continue; }
    if (ch === ")") { tokens.push({ type: "RPAREN" }); i++; continue; }
    if (/\d/.test(ch)) {
      let n = "";
      while (i < s.length && /\d/.test(s[i])) { n += s[i]; i++; }
      tokens.push({ type: "NUM", value: parseInt(n, 10) });
      continue;
    }
    if (s.slice(i, i + 3).toUpperCase() === "AND") {
      const after = s[i + 3];
      if (!after || !/\w/.test(after)) { tokens.push({ type: "AND" }); i += 3; continue; }
    }
    if (s.slice(i, i + 2).toUpperCase() === "OR") {
      const after = s[i + 2];
      if (!after || !/\w/.test(after)) { tokens.push({ type: "OR" }); i += 2; continue; }
    }
    throw new Error(`Unexpected character at position ${i}: "${ch}"`);
  }
  tokens.push({ type: "EOF" });
  return tokens;
}

// ---- Recursive descent parser ----
// Grammar (AND binds tighter than OR):
//   expr   = term (OR term)*
//   term   = factor (AND factor)*
//   factor = "(" expr ")" | NUM

function parseTokens(tokens: Token[]): ASTNode {
  let pos = 0;

  const peek = (): Token => tokens[pos] ?? { type: "EOF" };
  const consume = (): Token => tokens[pos++] ?? { type: "EOF" };
  const expect = (type: TokenType): Token => {
    const t = consume();
    if (t.type !== type) throw new Error(`Expected ${type} but got ${t.type}`);
    return t;
  };

  function parseExpr(): ASTNode {
    let left: ASTNode = parseTerm();
    while (peek().type === "OR") {
      consume();
      const right = parseTerm();
      if (left.type === "OR") {
        (left as ASTBinary).children.push(right);
      } else {
        left = { type: "OR", children: [left, right] };
      }
    }
    return left;
  }

  function parseTerm(): ASTNode {
    let left: ASTNode = parseFactor();
    while (peek().type === "AND") {
      consume();
      const right = parseFactor();
      if (left.type === "AND") {
        (left as ASTBinary).children.push(right);
      } else {
        left = { type: "AND", children: [left, right] };
      }
    }
    return left;
  }

  function parseFactor(): ASTNode {
    const t = peek();
    if (t.type === "LPAREN") {
      consume();
      const node = parseExpr();
      expect("RPAREN");
      return node;
    }
    if (t.type === "NUM") {
      consume();
      return { type: "NUM", value: t.value! };
    }
    if (t.type === "EOF") throw new Error("Unexpected end of expression");
    throw new Error(`Unexpected token: ${t.type}`);
  }

  const result = parseExpr();
  if (peek().type !== "EOF") throw new Error("Unexpected content after expression");
  return result;
}

// ---- AST helpers ----

function astToFilterGroup(ast: ASTNode, flatRules: FlatRule[]): any {
  if (ast.type === "NUM") {
    const r = flatRules.find((r) => r.id === ast.value);
    if (!r) throw new Error(`Rule ${ast.value} not found in rules list`);
    return { field: r.field, operator: r.operator, value: r.value };
  }
  return {
    logic: ast.type as "AND" | "OR",
    rules: (ast as ASTBinary).children.map((c) => astToFilterGroup(c, flatRules)),
  };
}

function pruneASTNode(ast: ASTNode, removedId: number): ASTNode | null {
  if (ast.type === "NUM") return ast.value === removedId ? null : ast;
  const pruned = (ast as ASTBinary).children
    .map((c) => pruneASTNode(c, removedId))
    .filter(Boolean) as ASTNode[];
  if (pruned.length === 0) return null;
  if (pruned.length === 1) return pruned[0];
  return { type: ast.type as "AND" | "OR", children: pruned };
}

function renumberASTNode(ast: ASTNode, removedId: number): ASTNode {
  if (ast.type === "NUM") {
    return { type: "NUM", value: ast.value > removedId ? ast.value - 1 : ast.value };
  }
  return {
    type: ast.type as "AND" | "OR",
    children: (ast as ASTBinary).children.map((c) => renumberASTNode(c, removedId)),
  };
}

function astToExprString(ast: ASTNode, parentType?: "AND" | "OR"): string {
  if (ast.type === "NUM") return String(ast.value);
  const bin = ast as ASTBinary;
  const parts = bin.children.map((c) => astToExprString(c, bin.type));
  const joined = parts.join(` ${bin.type} `);
  // Add parens when OR is nested inside AND (lower precedence inside higher)
  if (parentType === "AND" && bin.type === "OR") return `(${joined})`;
  return joined;
}

// ---- Public API ----

/**
 * Parse a logic expression like "1 AND (2 OR 3)" and flat rules into a FilterGroup tree.
 * Throws if the expression is invalid.
 */
export function parseExprToFilterGroup(expr: string, rules: FlatRule[]): FilterGroup {
  if (!expr.trim()) {
    return { logic: "AND", rules: rules.map((r) => ({ field: r.field, operator: r.operator, value: r.value })) };
  }
  const tokens = tokenize(expr.trim());
  const ast = parseTokens(tokens);
  const result = astToFilterGroup(ast, rules);
  if ("logic" in result) return result as FilterGroup;
  return { logic: "AND", rules: [result as any] };
}

/**
 * Convert a FilterGroup tree to a flat list of rules + a logic expression string.
 */
export function filterGroupToFlat(fg: FilterGroup): FlatFilterSet {
  let counter = 0;
  const rules: FlatRule[] = [];

  function nodeToExpr(node: any): string {
    if (node && "field" in node) {
      counter++;
      rules.push({ id: counter, field: node.field, operator: node.operator ?? node.op ?? "equals", value: node.value });
      return String(counter);
    }
    const g = node as FilterGroup;
    if (!g.rules || g.rules.length === 0) return "";
    if (g.rules.length === 1) return nodeToExpr(g.rules[0]);
    const subExprs = g.rules.map((r) => nodeToExpr(r)).filter(Boolean);
    if (subExprs.length === 1) return subExprs[0];
    return `(${subExprs.join(` ${g.logic} `)})`;
  }

  let expr = nodeToExpr(fg);

  // Strip single set of outer parens if they wrap the whole expression
  if (expr.startsWith("(") && expr.endsWith(")")) {
    let depth = 0;
    let isOuter = true;
    for (let i = 0; i < expr.length - 1; i++) {
      if (expr[i] === "(") depth++;
      else if (expr[i] === ")") depth--;
      if (depth === 0 && i < expr.length - 1) { isOuter = false; break; }
    }
    if (isOuter) expr = expr.slice(1, -1);
  }

  return { rules, logicExpr: expr || (rules.length > 0 ? "1" : "") };
}

/**
 * Validate a logic expression. Returns null if valid, or an error message string.
 */
export function validateExpr(expr: string, numRules: number): string | null {
  if (!expr.trim()) return "Expression cannot be empty";

  // Check balanced parentheses
  let depth = 0;
  for (const ch of expr) {
    if (ch === "(") depth++;
    else if (ch === ")") {
      depth--;
      if (depth < 0) return "Unbalanced parentheses: unexpected closing parenthesis";
    }
  }
  if (depth !== 0) return "Unbalanced parentheses: missing closing parenthesis";

  try {
    const tokens = tokenize(expr);
    const ast = parseTokens(tokens);

    const referenced = new Set<number>();
    function collect(node: ASTNode) {
      if (node.type === "NUM") referenced.add(node.value);
      else (node as ASTBinary).children.forEach(collect);
    }
    collect(ast);

    for (const n of referenced) {
      if (n < 1 || n > numRules) {
        return `Filter ${n} does not exist (you have ${numRules} filter${numRules === 1 ? "" : "s"})`;
      }
    }
  } catch (e: any) {
    return e.message || "Invalid expression";
  }

  return null;
}

/**
 * Generate the default AND expression for N rules: "1 AND 2 AND ... AND N"
 */
export function defaultExpr(numRules: number): string {
  if (numRules === 0) return "";
  if (numRules === 1) return "1";
  return Array.from({ length: numRules }, (_, i) => i + 1).join(" AND ");
}

/**
 * Update expression after removing rule at 1-indexed position removedId.
 * Renumbers all higher indices. Returns new expression string (may be "").
 */
export function removeRuleFromExpr(expr: string, removedId: number): string {
  if (!expr.trim()) return "";
  try {
    const tokens = tokenize(expr.trim());
    const ast = parseTokens(tokens);
    const pruned = pruneASTNode(ast, removedId);
    if (!pruned) return "";
    const renumbered = renumberASTNode(pruned, removedId);
    return astToExprString(renumbered);
  } catch {
    return "";
  }
}

/**
 * Append a new rule (AND newId) to the existing expression.
 */
export function addRuleToExpr(expr: string, newId: number): string {
  if (!expr.trim()) return String(newId);
  return `${expr} AND ${newId}`;
}

/**
 * Check if a logic string is a Salesforce-style expression (contains digits)
 * vs. a simple "AND"/"OR".
 */
export function isLogicExpr(logic: string): boolean {
  return /\d/.test(logic ?? "");
}
