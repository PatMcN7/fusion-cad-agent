// No browser/network dependencies. Avoid displaying the privileged first preset
// when the server reports a bare model ID from its environment configuration.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const source = html.slice(html.indexOf('function selectCurrentModel('), html.indexOf('async function boot(){'));
const select = {options: [], appendChild(o) { this.options.push(o); }};
function option(value, provider) {
  const o = {value, dataset: {provider}, _selected: false};
  Object.defineProperty(o, 'selected', {get() { return this._selected; }, set(v) {
    if (v) for (const other of select.options) other._selected = false;
    this._selected = v;
  }});
  return o;
}
select.options.push(option('hybrid-dev:gpt-6-astra','hybrid_dev'), option('hybrid:gpt-6-astra','hybrid'), option('codex:gpt-6-astra','codex'));
select.options[0].selected = true;
const context = {$: () => select, document: {createElement: () => option('', '')}};
vm.createContext(context); vm.runInContext(source, context);
context.selectCurrentModel('hybrid', 'gpt-6-astra');
assert.equal(select.options.find(o => o.selected).dataset.provider, 'hybrid');
context.selectCurrentModel('codex', 'gpt-6-astra');
assert.equal(select.options.find(o => o.selected).value, 'codex:gpt-6-astra');
context.selectCurrentModel('openai', 'custom-model');
assert.equal(select.options.find(o => o.selected).dataset.provider, 'openai');
assert.equal(select.options.find(o => o.selected).value, 'custom-model');
context.selectCurrentModel('hybrid_dev', 'hybrid-dev:gpt-6-astra');
assert.equal(select.options.find(o => o.selected).dataset.provider, 'hybrid_dev');
console.log('Model picker provider/permission matching passed');
