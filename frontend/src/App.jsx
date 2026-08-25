import React, { useState, useEffect, useCallback } from 'react';
import StatCards from './components/StatCards';
import FunnelChart from './components/FunnelChart';
import RootCauseChart from './components/RootCauseChart';
import AuditFeed from './components/AuditFeed';
import RunBatchButton from './components/RunBatchButton';
import EvalTable from './components/EvalTable';
import { getStats, getFunnel, getRootCauses, getAuditLog } from './api';

const NAV_ITEMS = [
  { id: 'dashboard', label: 'Dashboard', icon: '📊' },
  { id: 'audit', label: 'Audit Log', icon: '📋' },
  { id: 'eval', label: 'Evaluation', icon: '🎯' },
];

export default function App() {
  const [page, setPage] = useState('dashboard');
  const [stats, setStats] = useState(null);
  const [funnel, setFunnel] = useState(null);
  const [rootCauses, setRootCauses] = useState(null);
  const [auditLog, setAuditLog] = useState([]);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState(null);

  const fetchAll = useCallback(async () => {
    try {
      const [s, f, r, a] = await Promise.all([
        getStats().catch(() => null),
        getFunnel().catch(() => null),
        getRootCauses().catch(() => null),
        getAuditLog(50).catch(() => []),
      ]);
      setStats(s);
      setFunnel(f);
      setRootCauses(r);
      setAuditLog(a);
      setLastUpdated(new Date());
    } catch (err) {
      console.error('Fetch error:', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 10000);
    return () => clearInterval(id);
  }, [fetchAll]);

  return (
    <div className="flex h-screen bg-gray-950 text-slate-100">
      {/* Sidebar */}
      <aside className="w-64 bg-sidebar border-r border-slate-800 flex flex-col shrink-0">
        <div className="p-5 border-b border-slate-800">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-accent flex items-center justify-center font-bold text-slate-950 text-lg">R</div>
            <div>
              <h1 className="font-bold text-sm text-white tracking-tight">Revenue Recovery</h1>
              <p className="text-[10px] text-accent font-mono font-medium">AI AGENT</p>
            </div>
          </div>
        </div>
        <nav className="flex-1 p-3 space-y-1">
          {NAV_ITEMS.map((item) => (
            <button
              key={item.id}
              onClick={() => setPage(item.id)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-xs font-medium transition-colors ${
                page === item.id
                  ? 'bg-accent/10 text-accent border border-accent/30'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'
              }`}
            >
              <span>{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="p-3 m-3 bg-slate-900/80 rounded-lg border border-slate-800">
          <div className="flex items-center gap-2">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
            </span>
            <span className="text-[11px] font-mono text-slate-300">Agent Online</span>
          </div>
          {lastUpdated && (
            <p className="text-[10px] text-slate-500 mt-1 font-mono">Updated {lastUpdated.toLocaleTimeString()}</p>
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-y-auto">
        <header className="sticky top-0 z-10 h-14 px-6 border-b border-slate-800 bg-gray-950/80 backdrop-blur flex items-center justify-between">
          <h2 className="text-base font-semibold text-white">
            {page === 'dashboard' ? 'Dashboard' : page === 'audit' ? 'Audit Log' : 'Model Evaluation'}
          </h2>
          <RunBatchButton onComplete={fetchAll} />
        </header>

        <div className="p-6 max-w-7xl mx-auto space-y-6">
          {loading && <p className="text-slate-500 font-mono text-sm">Loading dashboard data...</p>}

          {page === 'dashboard' && (
            <>
              <StatCards stats={stats} />
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5">
                  <h3 className="text-sm font-semibold text-white mb-4">Recovery Funnel</h3>
                  <FunnelChart data={funnel} />
                </div>
                <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-5">
                  <h3 className="text-sm font-semibold text-white mb-4">Root Cause Breakdown</h3>
                  <RootCauseChart data={rootCauses} />
                </div>
              </div>
              <AuditFeed logs={auditLog} />
            </>
          )}

          {page === 'audit' && <AuditFeed logs={auditLog} />}
          {page === 'eval' && <EvalTable />}
        </div>
      </main>
    </div>
  );
}
