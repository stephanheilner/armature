import { api, listOf } from '../api.js';
import { attempt, card, clear, confirmDialog, element, empty, errorBox, loading, pageHeader, table } from '../dom.js';
import { hashFor } from '../router.js';
import { parseJson } from '../schema.js';

const INSTANCE = 'instance';

export const scopeParam = (scope) => (scope === '*' || scope === INSTANCE || !scope ? INSTANCE : String(scope));

export function settingText(value) {
  return JSON.stringify(value, null, 2) ?? 'null';
}

function settingForm(scopes, setting, onSaved, onCancel) {
  const scopeSelect = element('select', { id: 'setting-scope', disabled: Boolean(setting) }, scopes.map(([id, label]) => element('option', { value: id }, label)));
  scopeSelect.value = setting ? scopeParam(setting.scope) : INSTANCE;
  const keyInput = element('input', { type: 'text', id: 'setting-key', required: true, maxlength: 191, spellcheck: 'false', autocapitalize: 'none', readonly: Boolean(setting) });
  keyInput.value = setting?.key || '';
  const valueInput = element('textarea', { id: 'setting-value', class: 'mono', rows: 8, spellcheck: 'false', 'aria-describedby': 'setting-value-check' });
  valueInput.value = setting ? settingText(setting.value) : '';
  const check = element('span', { class: 'note', id: 'setting-value-check', 'aria-live': 'polite' });
  const saveButton = element('button', { type: 'submit', class: 'primary' }, setting ? 'Save setting' : 'Add setting');
  const status = element('div', { role: 'status' });
  const validate = () => {
    const parsed = parseJson(valueInput.value);
    check.textContent = parsed.error ? `Not valid JSON${parsed.line ? ` at line ${parsed.line}, column ${parsed.col}` : ''}: ${parsed.error}` : 'Valid JSON.';
    check.classList.toggle('bad', Boolean(parsed.error));
    valueInput.setAttribute('aria-invalid', parsed.error ? 'true' : 'false');
    saveButton.disabled = Boolean(parsed.error);
    return parsed;
  };
  valueInput.addEventListener('input', validate);
  if (setting) validate();
  const form = element('form', { class: 'stacked-form' },
    element('h2', { class: 'inline' }, setting ? `Edit ${setting.key}` : 'Add a setting'),
    element('label', { class: 'stacked', for: scopeSelect.id }, 'Scope', scopeSelect),
    element('label', { class: 'stacked', for: keyInput.id }, 'Key', keyInput),
    element('label', { class: 'stacked', for: valueInput.id }, 'Value, as JSON', valueInput, check),
    status, element('div', { class: 'wf-actions' }, saveButton, element('button', { type: 'button', onclick: onCancel }, 'Cancel')));
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const parsed = validate();
    if (parsed.error) return;
    const key = keyInput.value.trim();
    const saved = await attempt(status, saveButton, () => api('/api/settings', { method: 'POST', body: { scope: scopeSelect.value, key, value: parsed.value } }));
    if (saved) onSaved(`Saved ${key}.`);
  });
  requestAnimationFrame(() => (setting ? valueInput : keyInput).focus());
  return card(form);
}

export async function render(root, { query }) {
  root.append(pageHeader('Settings'));
  root.append(element('p', { class: 'note' }, 'Instance and team settings, stored as JSON. Secrets do not belong here. Keep them on the Secrets page.'));
  const teams = await api('/api/teams').then((payload) => listOf(payload, 'teams')).catch(() => []);
  const scopes = [[INSTANCE, 'Instance'], ...teams.map((team) => [String(team.id), team.name])];
  const scopeName = (scope) => scopes.find(([id]) => id === scopeParam(scope))?.[1] || scope;
  const filter = query.get('scope') || '';
  const filterSelect = element('select', { id: 'settings-scope' }, element('option', { value: '' }, 'All scopes'), scopes.map(([id, label]) => element('option', { value: id }, label)));
  filterSelect.value = filter;
  filterSelect.addEventListener('change', () => {
    location.hash = hashFor('settings', [], { scope: filterSelect.value });
  });
  const addButton = element('button', { type: 'button', class: 'primary' }, 'Add setting');
  const status = element('div', { role: 'status' });
  const formSlot = element('div');
  const listSlot = element('div', {}, loading());
  root.append(element('div', { class: 'filters' }, element('label', { for: filterSelect.id }, 'Scope', filterSelect), addButton), status, formSlot, listSlot);

  const closeForm = () => clear(formSlot);
  const openForm = (setting) => {
    clear(status);
    clear(formSlot).append(settingForm(scopes, setting, (message) => {
      closeForm();
      reload(message);
    }, closeForm));
  };
  addButton.addEventListener('click', () => openForm(null));

  async function reload(message) {
    let settings;
    try {
      const params = new URLSearchParams(filter ? { scope: filter } : {});
      settings = listOf(await api(`/api/settings?${params}`), 'settings');
    } catch (err) {
      clear(listSlot).append(errorBox(err));
      return;
    }
    clear(status);
    if (message) status.append(element('p', { class: 'note' }, message));
    clear(listSlot).append(card(settings.length ? table(['Scope', 'Key', 'Value', ''], settings.map((setting) => {
      const editButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Edit ${setting.key}` }, 'Edit');
      editButton.addEventListener('click', () => openForm(setting));
      const deleteButton = element('button', { type: 'button', class: 'link-button', 'aria-label': `Delete ${setting.key}` }, 'Delete');
      deleteButton.addEventListener('click', async () => {
        if (!(await confirmDialog(`Delete ${setting.key}?`, `The ${scopeName(setting.scope)} setting goes back to its default.`, 'Delete'))) return;
        const deleted = await attempt(status, deleteButton, () => api('/api/settings/delete', { method: 'POST', body: { scope: scopeParam(setting.scope), key: setting.key } }));
        if (deleted) reload(`Deleted ${setting.key}.`);
      });
      return [scopeName(setting.scope), element('span', { class: 'mono' }, setting.key), element('pre', { class: 'setting-value' }, settingText(setting.value)),
        element('span', { class: 'row-actions' }, editButton, ' ', deleteButton)];
    })) : empty(filter ? 'No settings in this scope.' : 'No settings yet.')));
  }

  await reload();
}
