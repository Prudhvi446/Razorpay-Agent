import React from 'react';

const formatCurrency = (paise) => {
  const rupees = (paise || 0) / 100;
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 0,
  }).format(rupees);
};

const cards = [
  { key: 'total_at_risk', label: 'Total At Risk', color: 'text-red-400', bg: 'bg-red-500/10', border: 'border-red-500/20', isCurrency: true },
  { key: 'total_recovered', label: 'Total Recovered', color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/20', isCurrency: true },
  { key: 'recovery_rate', label: 'Overall Recovery Rate', color: 'text-amber-400', bg: 'bg-amber-500/10', border: 'border-amber-500/20', suffix: '%' },
  { key: 'active_recoveries', label: 'Active Recoveries', color: 'text-blue-400', bg: 'bg-blue-500/10', border: 'border-blue-500/20' },
];

export default function StatCards({ stats }) {
  if (!stats) return null;

  const ab = stats.ab_testing || {
    control_group: { recovery_rate: 0, total_recovered: 0, total_at_risk: 0, count: 0 },
    ai_group: { recovery_rate: 0, total_recovered: 0, total_at_risk: 0, count: 0 },
    incremental_lift_pct: 0,
    incremental_revenue: 0,
  };

  const ctrl = ab.control_group || {};
  const ai = ab.ai_group || {};
  const liftPct = ab.incremental_lift_pct ?? 0;
  const isPositiveLift = liftPct >= 0;

  return (
    <div className="space-y-5">
      {/* Primary KPI Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {cards.map((card) => (
          <div key={card.key} className={`${card.bg} ${card.border} border rounded-xl p-5 transition hover:scale-[1.01]`}>
            <p className="text-xs text-slate-400 font-medium uppercase tracking-wider">{card.label}</p>
            <p className={`text-2xl font-bold mt-2 font-mono ${card.color}`}>
              {card.isCurrency ? formatCurrency(stats[card.key]) : `${stats[card.key] ?? 0}${card.suffix || ''}`}
            </p>
          </div>
        ))}
      </div>

      {/* A/B Testing Comparative Matrix & Incremental Lift Card */}
      <div className="bg-slate-900/70 border border-slate-800 rounded-xl p-5">
        <div className="flex flex-wrap items-center justify-between gap-2 mb-4 pb-3 border-b border-slate-800/80">
          <div className="flex items-center gap-2">
            <span className="px-2.5 py-0.5 rounded-full text-[11px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 tracking-wide uppercase">
              A/B Evaluation Matrix
            </span>
            <h3 className="text-sm font-semibold text-white">Control Group vs. AI Recovery Agent</h3>
          </div>
          <span className="text-xs text-slate-400 font-mono">
            {ctrl.count || 0} Control txns • {ai.count || 0} AI txns
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Control Group */}
          <div className="bg-slate-950/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Control Group</span>
                <span className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-300 font-mono">Static Rules</span>
              </div>
              <div className="mt-3">
                <p className="text-xs text-slate-500">Recovery Rate</p>
                <p className="text-2xl font-bold font-mono text-slate-300">
                  {ctrl.recovery_rate ?? 0}%
                </p>
              </div>
            </div>
            <div className="mt-4 pt-3 border-t border-slate-800/60 flex items-center justify-between text-xs">
              <span className="text-slate-400">Total Recovered:</span>
              <span className="font-mono font-semibold text-slate-200">{formatCurrency(ctrl.total_recovered)}</span>
            </div>
          </div>

          {/* AI Group */}
          <div className="bg-emerald-950/20 border border-emerald-500/30 rounded-xl p-4 flex flex-col justify-between">
            <div>
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-emerald-400 uppercase tracking-wider">AI Recovery Group</span>
                <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 font-mono">
                  LangGraph Agent
                </span>
              </div>
              <div className="mt-3">
                <p className="text-xs text-emerald-400/80">Recovery Rate</p>
                <p className="text-2xl font-bold font-mono text-emerald-400">
                  {ai.recovery_rate ?? 0}%
                </p>
              </div>
            </div>
            <div className="mt-4 pt-3 border-t border-emerald-500/20 flex items-center justify-between text-xs">
              <span className="text-slate-300">Total Recovered:</span>
              <span className="font-mono font-semibold text-emerald-300">{formatCurrency(ai.total_recovered)}</span>
            </div>
          </div>

          {/* Incremental Revenue Lift (%) Highlighted Metric */}
          <div className="relative overflow-hidden bg-gradient-to-br from-indigo-950/40 via-teal-950/30 to-emerald-950/40 border border-teal-500/40 rounded-xl p-4 flex flex-col justify-between shadow-lg shadow-teal-950/20">
            <div>
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-teal-300 uppercase tracking-wider">Incremental Lift</span>
                <span className="text-xs">🚀</span>
              </div>
              <div className="mt-2">
                <p className="text-xs text-slate-300 font-medium">Recovery Lift vs. Control</p>
                <div className="flex items-baseline gap-2 mt-1">
                  <span className={`text-3xl font-extrabold font-mono ${isPositiveLift ? 'text-teal-400' : 'text-amber-400'}`}>
                    {isPositiveLift ? `+${liftPct}%` : `${liftPct}%`}
                  </span>
                </div>
              </div>
            </div>
            <div className="mt-4 pt-3 border-t border-teal-500/20 flex items-center justify-between text-xs">
              <span className="text-slate-300">Lift Unlocked:</span>
              <span className="font-mono font-bold text-teal-300">
                +{formatCurrency(ab.incremental_revenue || 0)}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

