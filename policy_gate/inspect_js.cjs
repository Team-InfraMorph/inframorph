// Parse data only. Never require/import code from the target repository.
const acorn = require('./vendor/acorn.cjs');
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 24 * 1024 * 1024) process.exit(2); });
process.stdin.on('end', () => {
  try {
    const files = JSON.parse(input), result = {};
    for (const [name, source] of Object.entries(files)) {
      let ast;
      try { ast = acorn.parse(source, {ecmaVersion: 2022, locations: true, sourceType: name.endsWith('.mjs') ? 'module' : 'script'}); }
      catch { result[name] = {error: 'javascript_syntax_invalid'}; continue; }
      let forbidden = false;
      const strings = [], calls = [];
      function visit(n) {
        if (!n || typeof n !== 'object') return;
        if (n.type === 'Literal' && typeof n.value === 'string') strings.push({value:n.value, start:n.start, end:n.end});
        // Conservative capability references: aliases cannot bypass the check.
        if (n.type === 'Identifier' && ['eval', 'Function'].includes(n.name)) forbidden = true;
        if (n.type === 'Literal' && typeof n.value === 'string' && /^(node:)?child_process$/.test(n.value)) {
          // Only dependency literals are checked below; arbitrary documentation strings are harmless.
        }
        if (['ImportDeclaration','ExportNamedDeclaration','ExportAllDeclaration'].includes(n.type) && /^(node:)?child_process$/.test(n.source?.value || '')) forbidden = true;
        if (n.type === 'ImportExpression' && /^(node:)?child_process$/.test(n.source?.value || '')) forbidden = true;
        if (n.type === 'CallExpression') {
          const c = n.callee;
          if (c.type === 'Identifier' && c.name === 'require' && /^(node:)?child_process$/.test(n.arguments[0]?.value || '')) forbidden = true;
          if (c.type === 'MemberExpression' && ['globalThis','global','window'].includes(c.object?.name) && ['eval','Function'].includes(c.property?.name || c.property?.value)) forbidden = true;
          calls.push({name: c.name || c.property?.name || '', start:n.start, end:n.end});
        }
        for (const [k,v] of Object.entries(n)) if (!['start','end','raw','loc'].includes(k)) {
          if (Array.isArray(v)) v.forEach(visit); else visit(v);
        }
      }
      visit(ast);
      // Recognize only direct top-level starts linked to a defined function.
      // Arbitrary calls, nested/dead code, strings and comments cannot be starts.
      const functions = new Set(), bindings = new Map(), reassigned = new Set();
      const bind = name => { if (name) bindings.set(name, (bindings.get(name) || 0) + 1); };
      for (const node of ast.body) {
        if (node.type === 'FunctionDeclaration' && node.id) {
          bind(node.id.name); functions.add(node.id.name);
        }
        if (node.type === 'VariableDeclaration') for (const item of node.declarations) {
          if (item.id.type === 'Identifier') {
            bind(item.id.name);
            if (['FunctionExpression','ArrowFunctionExpression'].includes(item.init?.type)) functions.add(item.id.name);
          } else {
            // Conservatively treat destructured names as bindings too.
            const names = pattern => {
              if (!pattern) return;
              if (pattern.type === 'Identifier') bind(pattern.name);
              else if (pattern.type === 'RestElement') names(pattern.argument);
              else if (pattern.type === 'AssignmentPattern') names(pattern.left);
              else if (pattern.type === 'ArrayPattern') pattern.elements.forEach(names);
              else if (pattern.type === 'ObjectPattern') pattern.properties.forEach(p => names(p.type === 'RestElement' ? p.argument : p.value));
            };
            names(item.id);
          }
        }
        if (node.type === 'ClassDeclaration') bind(node.id?.name);
        if (node.type === 'ImportDeclaration') node.specifiers.forEach(s => bind(s.local.name));
      }
      function assignments(node) {
        if (!node || typeof node !== 'object') return;
        const writes = pattern => {
          if (!pattern) return;
          if (pattern.type === 'Identifier') reassigned.add(pattern.name);
          else if (pattern.type === 'RestElement') writes(pattern.argument);
          else if (pattern.type === 'AssignmentPattern') writes(pattern.left);
          else if (pattern.type === 'ArrayPattern') pattern.elements.forEach(writes);
          else if (pattern.type === 'ObjectPattern') pattern.properties.forEach(p => writes(p.type === 'RestElement' ? p.argument : p.value));
          else if (pattern.type === 'MemberExpression' && ['globalThis','global','window'].includes(pattern.object?.name) &&
                   (pattern.property?.name || pattern.property?.value) === 'setInterval') reassigned.add('setInterval');
        };
        if (node.type === 'AssignmentExpression') writes(node.left);
        if (node.type === 'UpdateExpression' && node.argument.type === 'Identifier') reassigned.add(node.argument.name);
        for (const [key, value] of Object.entries(node)) if (!['start','end','raw','loc'].includes(key)) {
          if (Array.isArray(value)) value.forEach(assignments); else assignments(value);
        }
      }
      assignments(ast);
      const worker_starts = [];
      function start(call) {
        if (call?.type !== 'CallExpression' || call.optional || call.callee.type !== 'Identifier') return;
        let symbol = call.callee.name, kind = 'direct';
        if (symbol === 'setInterval') {
          if (bindings.has(symbol) || reassigned.has(symbol) || call.arguments.length !== 2 ||
              call.arguments[0].type !== 'Identifier' || call.arguments[1].type !== 'Literal' ||
              typeof call.arguments[1].value !== 'number' || call.arguments[1].value <= 0) return;
          symbol = call.arguments[0].name; kind = 'interval';
        } else if (call.arguments.length) return;
        if (!functions.has(symbol) || bindings.get(symbol) !== 1 || reassigned.has(symbol)) return;
        const tokens = [call.callee, ...(kind === 'interval' ? [call.arguments[0]] : [])];
        const lines = [...new Set(tokens.flatMap(t => Array.from({length:t.loc.end.line-t.loc.start.line+1}, (_,i) => t.loc.start.line+i)))].sort((a,b)=>a-b);
        worker_starts.push({kind, function:symbol, start_line:Math.min(...lines), end_line:Math.max(...lines), lines});
      }
      for (const node of ast.body) {
        if (node.type === 'ExpressionStatement') start(node.expression);
        if (node.type === 'VariableDeclaration') node.declarations.forEach(item => start(item.init));
      }
      const normalized = JSON.stringify(ast, (k,v) => ['start','end','raw','loc'].includes(k) ? undefined : typeof v === 'bigint' ? v.toString() : v);
      result[name] = {normalized, forbidden, strings, calls, worker_starts};
    }
    process.stdout.write(JSON.stringify(result));
  } catch { process.stdout.write(JSON.stringify({error:'javascript_parser_failed'})); process.exitCode = 1; }
});
