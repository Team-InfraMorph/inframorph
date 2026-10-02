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
      try { ast = acorn.parse(source, {ecmaVersion: 2022, sourceType: name.endsWith('.mjs') ? 'module' : 'script'}); }
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
        for (const [k,v] of Object.entries(n)) if (!['start','end','raw'].includes(k)) {
          if (Array.isArray(v)) v.forEach(visit); else visit(v);
        }
      }
      visit(ast);
      const normalized = JSON.stringify(ast, (k,v) => ['start','end','raw'].includes(k) ? undefined : typeof v === 'bigint' ? v.toString() : v);
      result[name] = {normalized, forbidden, strings, calls};
    }
    process.stdout.write(JSON.stringify(result));
  } catch { process.stdout.write(JSON.stringify({error:'javascript_parser_failed'})); process.exitCode = 1; }
});
