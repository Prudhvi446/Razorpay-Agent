import React, { useState } from 'react';

const ACTION_COLORS = {
  diagnosis_completed: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  decision_made: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  action_executed: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  escalation: 'bg-red-500/20 text-red-400 border-red-500/30',
  webhook_received: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  batch_run_completed: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  payment_link_created: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  email_sent: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
};

const DEFAULT_COLOR = 'bg-slate-500/20 text-slate-400 border-slate-500/30';

function formatIST(log) {
  if (log.timestamp_display) {
    return log.timestamp_display;
  }
  const raw = log.timestamp_ist || log.timestamp;
  if (!raw) return '';
  const hasTimezone = raw.endsWith('Z') || /[+-]\d{2}:\d{2}$/.test(raw);
  const dateStr = hasTimezone ? raw : raw + 'Z';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return raw;
  return d.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    day: '2-digit',
    month: 'short',
  }) + ' IST';
}

function LogEntry({ log }) {
  const [expanded, setExpanded] = useState(false);
  const colorClass = ACTION_COLORS[log.action] || DEFAULT_COLOR;
  const ts = formatIST(log);

  return (
    <div className="flex items-start gap-3 py-2 px-2 rounded hover:bg-slate-900/50 transition">
      <span className="text-slate-500 text-[11px] whitespace-nowrap shrink-0 mt-0.5">{ts}</span>
      <span className={`px-1.5 py-0.5 rounded text-[10px] uppercase font-bold shrink-0 border ${colorClass}`}>
        {log.action?.replace(/_/g, ' ').slice(0, 16)}
      </span>
      <span className="text-[11px] text-slate-500 shrink-0">[{log.actor}]</span>
      <div className="flex-1 min-w-0">
        <p className={`text-slate-300 text-xs ${expanded ? '' : 'truncate'}`}>
          {log.reasoning || log.action}
        </p>
        {log.reasoning && log.reasoning.length > 80 && (
          <button onClick={() => setExpanded(!expanded)} className="text-[10px] text-accent mt-0.5 hover:underline">
            {expanded ? 'collapse' : 'expand'}
          </button>
        )}
      </div>
    </div>
  );
}

export default function AuditFeed({ logs }) {
  if (!logs || !logs.length) return <p className="text-slate-500 text-sm p-4 font-mono">No audit entries yet. Run the agent pipeline to generate logs.</p>;

  return (
    <div className="bg-slate-950/90 rounded-lg border border-slate-800 overflow-hidden">
      <div className="px-4 py-2.5 border-b border-slate-800 flex items-center justify-between bg-slate-900/90">
        <div className="flex items-center gap-2">
          <div className="flex gap-1.5">
            <div className="w-2.5 h-2.5 rounded-full bg-red-500/80"></div>
            <div className="w-2.5 h-2.5 rounded-full bg-amber-500/80"></div>
            <div className="w-2.5 h-2.5 rounded-full bg-emerald-500/80"></div>
          </div>
          <span className="text-xs font-semibold text-slate-200">Agent Audit Log</span>
        </div>
        <span className="text-[11px] font-mono text-emerald-400">● {logs.length} entries</span>
      </div>
      <div className="p-2 font-mono max-h-[400px] overflow-y-auto space-y-0.5">
        {logs.map((log, i) => <LogEntry key={log.id || i} log={log} />)}
      </div>
    </div>
  );
}
