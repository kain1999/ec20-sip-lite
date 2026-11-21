// dev-only sanity check for the static panel: ids and actions must line up
const fs = require('fs');
const html = fs.readFileSync('app/web/static/index.html', 'utf8');
const js = fs.readFileSync('app/web/static/app.js', 'utf8');
const css = fs.readFileSync('app/web/static/style.css', 'utf8');

const htmlIds = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));
// views are rendered from JS template strings, so those ids count as declared too
[...js.matchAll(/id="([A-Za-z0-9_-]+)"/g)].forEach((m) => htmlIds.add(m[1]));
const jsIds = [...js.matchAll(/\$\('#([A-Za-z0-9_-]+)'\)/g)].map((m) => m[1]);
const dynamicPrefixes = ['m-', 's-', 'kpi-'];
const missingIds = [...new Set(jsIds)].filter((id) => !htmlIds.has(id) &&
  !dynamicPrefixes.some((p) => id.startsWith(p)));

const actions = [...new Set([...js.matchAll(/data-action="([a-z-]+)"/g)].map((m) => m[1]))];
const cases = new Set([...js.matchAll(/case '([a-z-]+)':/g)].map((m) => m[1]));
const missingHandlers = actions.filter((a) => !cases.has(a));

const apiCalls = [...new Set([...js.matchAll(/'(\/api\/[a-z0-9_/{}]+)'/g)].map((m) => m[1]))];

console.log('html ids            :', htmlIds.size);
console.log('js ids missing      :', missingIds.length ? missingIds : 'none');
console.log('panel actions       :', actions.length, '| handlers:', cases.size);
console.log('actions w/o handler :', missingHandlers.length ? missingHandlers : 'none');
console.log('api endpoints used  :', apiCalls.length);
console.log('css bytes           :', css.length);
const ok = !missingIds.length && !missingHandlers.length && apiCalls.length > 10;
console.log(ok ? 'UI CHECK OK' : 'UI CHECK FAILED');
process.exit(ok ? 0 : 1);