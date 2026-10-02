import React, { useEffect, useMemo, useState } from 'react';

const initialMappings = {
  id: { type: 'copy', source_column: 'legacy_id' },
  code: { type: 'copy', source_column: 'code' },
  label: { type: 'trim', source_column: 'raw_name' },
};

const mappingTypes = [
  { value: 'copy', label: '复制 copy' },
  { value: 'trim', label: '去首尾空白 trim' },
  { value: 'decimal_int', label: '十进制整数 decimal_int' },
  { value: 'constant', label: '常量 constant' },
];

const sourceColumns = ['legacy_id', 'code', 'raw_name', 'note'];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = data.error?.message || response.statusText || '请求失败';
    throw Object.assign(new Error(message), { status: response.statusCode, data });
  }
  return data;
}

function MappingEditor({ mappings, onChange }) {
  const update = (field, patch) => {
    onChange({
      ...mappings,
      [field]: {
        ...mappings[field],
        // Selecting a mapping type must not leave a value from another type
        // attached to the mapping object sent to FastAPI.
        ...(patch.type && patch.type !== mappings[field].type ? { source_column: undefined, value: undefined } : {}),
        ...patch,
      },
    });
  };

  return (
    <div className="mapping-grid">
      {Object.entries(mappings).map(([field, mapping]) => (
        <label className="mapping-card" key={field}>
          <span className="field-name">新表字段：{field}</span>
          <select
            value={mapping.type}
            onChange={(event) => update(field, { type: event.target.value })}
          >
            {mappingTypes.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          {mapping.type === 'constant' ? (
            <input
              placeholder="常量值（id 字段需为整数）"
              value={mapping.value ?? ''}
              onChange={(event) => {
                const raw = event.target.value;
                update(field, { value: field === 'id' && raw !== '' ? Number(raw) : raw });
              }}
            />
          ) : (
            <select
              value={mapping.source_column || ''}
              onChange={(event) => update(field, { source_column: event.target.value })}
            >
              <option value="" disabled>选择旧表字段</option>
              {sourceColumns.map((column) => <option key={column} value={column}>{column}</option>)}
            </select>
          )}
        </label>
      ))}
    </div>
  );
}

function FailureTable({ failures }) {
  if (!failures?.length) return null;
  return (
    <section className="panel error-panel">
      <h2>预演失败行（{failures.length}）</h2>
      <p>以下问题逐行收集；本次预演没有生成可提交影子表，正式表保持不变。</p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>旧表 rowid</th>
              <th>legacy_id</th>
              <th>字段</th>
              <th>失败原因</th>
              <th>映射来源</th>
              <th>影子值</th>
            </tr>
          </thead>
          <tbody>
            {failures.flatMap((failure) =>
              failure.errors.map((error, index) => (
                <tr key={`${failure.row_number}-${index}`}>
                  {index === 0 && <td rowSpan={failure.errors.length}>{failure.row_number}</td>}
                  {index === 0 && <td rowSpan={failure.errors.length}>{failure.legacy_id ?? 'NULL'}</td>}
                  <td>{error.field}</td>
                  <td><code>{error.code}</code><div>{error.message}</div></td>
                  <td>{error.mapping}</td>
                  <td><code>{JSON.stringify(failure.values?.[error.field] ?? null)}</code></td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function DataTable({ title, rows, empty }) {
  return (
    <section className="panel">
      <h2>{title} <span>{rows.length} 行</span></h2>
      {rows.length === 0 ? <p>{empty}</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr>{Object.keys(rows[0]).map((key) => <th key={key}>{key}</th>)}</tr></thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>{Object.values(row).map((value, i) => <td key={i}>{JSON.stringify(value)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function versionKindLabel(version) {
  return version.kind === 'restore'
    ? `恢复（来自 #${version.source_version_id}）`
    : '字段迁移';
}

function DiffSummary({ diff }) {
  if (!diff) return null;
  return (
    <div className="diff-summary">
      <span className={diff.added_count ? 'diff-badge add' : 'diff-badge'}>候选新增 {diff.added_count ?? diff.added?.length ?? 0}</span>
      <span className={diff.removed_count ? 'diff-badge remove' : 'diff-badge'}>候选移除 {diff.removed_count ?? diff.removed?.length ?? 0}</span>
      <span className={diff.changed_count ? 'diff-badge change' : 'diff-badge'}>字段变化 {diff.changed_count ?? diff.changed?.length ?? 0}</span>
      {diff.truncated && <span className="diff-note">差异较多，仅展示前 200 项</span>}
    </div>
  );
}

function DiffTable({ diff }) {
  const changed = diff?.changed || [];
  if (!changed.length) return null;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>id</th><th>变化字段</th><th>当前正式表</th><th>候选（恢复后）</th></tr>
        </thead>
        <tbody>
          {changed.map((item) => (
            <tr key={item.id}>
              <td>{item.id}</td>
              <td>{item.columns.join(', ')}</td>
              <td><code>{JSON.stringify(item.current)}</code></td>
              <td><code>{JSON.stringify(item.candidate)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [state, setState] = useState(null);
  const [mappings, setMappings] = useState(initialMappings);
  const [preview, setPreview] = useState(null);
  const [restorePreview, setRestorePreview] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const [historyRows, setHistoryRows] = useState([]);
  const [selectedVersion, setSelectedVersion] = useState('');
  const [faults, setFaults] = useState({ preview_copy: false, commit_switch: false, restore_switch: false });
  const [newLegacy, setNewLegacy] = useState({ legacy_id: '', code: '', raw_name: '', note: '' });

  const refresh = async () => setState(await api('/api/state'));

  useEffect(() => { refresh().catch((error) => setMessage({ type: 'error', text: error.message })); }, []);

  useEffect(() => {
    if (!selectedVersion) {
      setHistoryRows([]);
      return;
    }
    api(`/api/history/${selectedVersion}/rows`)
      .then((data) => setHistoryRows(data.rows))
      .catch((error) => setMessage({ type: 'error', text: error.message }));
  }, [selectedVersion, state]);

  const latestVersion = useMemo(() => state?.history?.at(-1)?.version_id || '', [state]);
  useEffect(() => {
    if (state && selectedVersion === '' && latestVersion) setSelectedVersion(String(latestVersion));
  }, [state, selectedVersion, latestVersion]);

  // Drop restore preview whenever the chosen version changes.
  useEffect(() => {
    if (restorePreview && String(restorePreview.source.version_id) !== String(selectedVersion)) {
      setRestorePreview(null);
    }
  }, [selectedVersion, restorePreview]);

  const runPreview = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await api('/api/migrations/preview', {
        method: 'POST',
        body: JSON.stringify(mappings),
      });
      setPreview(result);
      await refresh();
      if (result.ok) {
        setMessage({ type: 'success', text: `预演通过，源表修订号 ${result.source_revision}，绑定正式表代际 ${result.records_generation}` });
      } else {
        setMessage({ type: 'error', text: '预演发现约束或映射失败，未改变正式表' });
      }
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const runCommit = async () => {
    if (!preview?.preview_id) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await api('/api/migrations/commit', {
        method: 'POST',
        body: JSON.stringify({
          preview_id: preview.preview_id,
          source_revision: preview.source_revision,
          records_generation: preview.records_generation,
        }),
      });
      setMessage({
        type: 'success',
        text: `提交成功：迁移 #${result.migration_id}，代际 ${result.base_generation} → ${result.new_generation}，保留旧版 #${result.old_version.version_id}`,
      });
      setPreview(null);
      await refresh();
    } catch (error) {
      if (error.status === 409 || error.status === 404) {
        setMessage({ type: 'error', text: `${error.message}。请重新预演后再提交。` });
        setPreview(null);
      } else {
        setMessage({ type: 'error', text: error.message });
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const runRestorePreview = async () => {
    if (!selectedVersion) return;
    setBusy(true);
    setMessage(null);
    setRestorePreview(null);
    try {
      const result = await api('/api/restores/preview', {
        method: 'POST',
        body: JSON.stringify({ version_id: Number(selectedVersion) }),
      });
      setRestorePreview(result);
      await refresh();
      const diff = result.diff;
      setMessage({
        type: 'success',
        text: `恢复预演通过：候选来自历史版本 #${result.source.version_id}（其代际 ${result.source.generation}），${result.row_count} 行；当前正式表代际 ${result.records_generation}，尚未切换。`,
      });
      if (diff.added_count + diff.removed_count + diff.changed_count === 0) {
        setMessage((prev) => ({ ...prev, text: `${prev.text} 候选与当前正式表内容一致，确认仍会推进代际并封存当前表。` }));
      }
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const runRestoreCommit = async () => {
    if (!restorePreview?.preview_id) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await api('/api/restores/commit', {
        method: 'POST',
        body: JSON.stringify({
          preview_id: restorePreview.preview_id,
          records_generation: restorePreview.records_generation,
        }),
      });
      setMessage({
        type: 'success',
        text: `恢复成功：代际 ${result.base_generation} → ${result.new_generation}，恢复来源 #${result.source_version_id}，恢复前的正式表已封存为 #${result.archived_version.version_id}（${result.archived_version.row_count} 行）`,
      });
      setRestorePreview(null);
      await refresh();
    } catch (error) {
      if (error.status === 409 || error.status === 404) {
        setMessage({ type: 'error', text: `${error.message}。请重新进行恢复预演。` });
        setRestorePreview(null);
      } else {
        setMessage({ type: 'error', text: error.message });
      }
      await refresh();
    } finally {
      setBusy(false);
    }
  };

  const addLegacyRow = async () => {
    setBusy(true);
    try {
      await api('/api/legacy', {
        method: 'POST',
        body: JSON.stringify({
          ...newLegacy,
          legacy_id: Number(newLegacy.legacy_id),
          raw_name: newLegacy.raw_name || null,
          note: newLegacy.note || null,
        }),
      });
      setNewLegacy({ legacy_id: '', code: '', raw_name: '', note: '' });
      setPreview(null);
      await refresh();
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  const toggleFault = async (name) => {
    const enabled = !faults[name];
    try {
      await api('/api/test/faults', { method: 'POST', body: JSON.stringify({ name, enabled }) });
      setFaults({ ...faults, [name]: enabled });
    } catch (error) {
      setMessage({ type: 'error', text: `故障注入需以 ALLOW_FAULT_INJECTION=1 启动 API：${error.message}` });
    }
  };

  const reset = async () => {
    setBusy(true);
    setMessage(null);
    try {
      await api('/api/admin/reset', { method: 'POST' });
      setPreview(null);
      setRestorePreview(null);
      setSelectedVersion('');
      setHistoryRows([]);
      await refresh();
    } catch (error) {
      setMessage({ type: 'error', text: error.message });
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <main className="app"><p>正在加载...</p></main>;

  const selectedMeta = state.history.find(
    (version) => String(version.version_id) === String(selectedVersion),
  );

  return (
    <main className="app">
      <header>
        <div>
          <h1>SQLite 旧记录影子表迁移 / 历史版本恢复</h1>
          <p>有限字段映射：复制、trim、十进制整数解析、常量；历史版本只读，可预演恢复</p>
        </div>
        <div className="badges">
          <div className="revision">源表修订号 <strong>{state.revision}</strong></div>
          <div className="revision generation">正式表代际 <strong>{state.records_generation}</strong></div>
        </div>
      </header>

      {message && <div className={`banner ${message.type}`}>{message.text}</div>}

      <section className="panel">
        <div className="panel-title">
          <h2>1. 字段映射与预演</h2>
          <div className="actions">
            <button disabled={busy} onClick={runPreview}>复制到影子表并预演</button>
            <button className="primary" disabled={busy || !preview?.preview_id} onClick={runCommit}>
              携带修订号与代际提交
            </button>
          </div>
        </div>
        <MappingEditor mappings={mappings} onChange={setMappings} />
        {preview && (
          <div className={`preview ${preview.ok ? 'ok' : 'bad'}`}>
            <strong>{preview.ok ? '预演通过' : '预演失败'}</strong>
            <span>preview_id: {preview.preview_id || '未保留'}</span>
            <span>依据 source_revision: {preview.source_revision}</span>
            <span>绑定正式表代际: {preview.records_generation}</span>
            <span>读取行数: {preview.row_count}</span>
          </div>
        )}
        <p className="hint">提交以预演所见的源表修订号和正式表代际裁决；期间发生迁移或恢复都会使旧预演失效。</p>
      </section>

      <FailureTable failures={preview?.failures} />

      <section className="panel two-column">
        <div>
          <h2>2. 交错写入旧表</h2>
          <div className="inline-form">
            {Object.keys(newLegacy).map((key) => (
              <input
                key={key}
                placeholder={key}
                value={newLegacy[key]}
                onChange={(event) => setNewLegacy({ ...newLegacy, [key]: event.target.value })}
              />
            ))}
            <button onClick={addLegacyRow} disabled={busy}>插入并推进修订号</button>
          </div>
        </div>
        <div>
          <h2>故障注入 / 测试</h2>
          <div className="faults">
            <button onClick={() => toggleFault('preview_copy')}>{faults.preview_copy ? '关闭' : '开启'} 复制后中断</button>
            <button onClick={() => toggleFault('commit_switch')}>{faults.commit_switch ? '关闭' : '开启'} 迁移切换前中断</button>
            <button onClick={() => toggleFault('restore_switch')}>{faults.restore_switch ? '关闭' : '开启'} 恢复切换前中断</button>
            <button onClick={reset}>重置数据库</button>
          </div>
          <p className="hint">影子/候选表：{state.shadow_tables.length ? state.shadow_tables.join(', ') : '无'}</p>
        </div>
      </section>

      <DataTable title="旧表 legacy_records" rows={state.legacy} empty="暂无旧数据" />
      <DataTable
        title={`正式表 records（代际 ${state.records_generation}）`}
        rows={state.records}
        empty="尚未迁移；首次提交后生成正式表"
      />

      <section className="panel">
        <div className="panel-title">
          <h2>只读历史版本（当前正式表代际 {state.records_generation}）</h2>
          <div className="actions">
            <button
              disabled={busy || !selectedVersion}
              onClick={runRestorePreview}
              title="从该版本生成候选正式表并核对差异，不改写任何记录"
            >
              恢复预演所选版本
            </button>
            <button
              className="primary"
              disabled={busy || !restorePreview?.preview_id}
              onClick={runRestoreCommit}
            >
              确认恢复（封存当前表并切换）
            </button>
          </div>
        </div>
        {state.history.length === 0 ? <p>尚无保留版本。</p> : (
          <>
            <select value={selectedVersion} onChange={(event) => setSelectedVersion(event.target.value)}>
              {state.history.map((version) => (
                <option key={version.version_id} value={version.version_id}>
                  {`版本 #${version.version_id} / ${versionKindLabel(version)} / 内容代际 ${version.generation} / ${version.row_count} 行${version.locked ? ' · 已封存只读' : ''}`}
                </option>
              ))}
            </select>
            {selectedMeta && (
              <p className="hint">
                {`所选：版本 #${selectedMeta.version_id}（${versionKindLabel(selectedMeta)}），封存的是代际 ${selectedMeta.generation} 的正式表，共 ${selectedMeta.row_count} 行。`}
              </p>
            )}
            <DataTable title={`历史版本 #${selectedVersion} 内容`} rows={historyRows} empty="该版本为空（首次迁移前无正式表）" />
          </>
        )}

        {restorePreview && (
          <div className="preview restore-preview">
            <strong>恢复预演（未切换）</strong>
            <span>恢复来源版本: #{restorePreview.source.version_id}（{restorePreview.source.kind === 'restore' ? '恢复产物' : '迁移产物'}，内容代际 {restorePreview.source.generation}）</span>
            <span>预演所见当前代际: {restorePreview.records_generation}</span>
            <span>候选行数: {restorePreview.row_count}</span>
            <DiffSummary diff={restorePreview.diff} />
            <DiffTable diff={restorePreview.diff} />
            <DataTable title="候选正式表内容" rows={restorePreview.candidate_rows} empty="候选表为空" />
            <p className="hint">
              确认时在同一 SQLite 事务内：封存当前正式表为新的历史版本 → 切换候选表为 records → 正式表代际 +1。
              预演后若代际变化，确认将被拒绝且本预演作废，需要重新预演。
            </p>
          </div>
        )}
      </section>
    </main>
  );
}
