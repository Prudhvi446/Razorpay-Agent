import React from 'react';

const formatCurrency = (paise) => {
  const rupees = (paise || 0) / 100;
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(rupees);
};

const cards = [
  { key: 'total_at_risk', label: 'Total At Risk', color: 'text-red-400', bg: 'bg-red-500/10', border: 'border-red-500/20', isCurrency: true },
  { key: 'total_recovered', label: 'Total Recovered', color: 'text-emerald-400', bg: 'bg-emerald-500/10', border: 'border-emerald-500/20', isCurrency: true },
  { key: 'recovery_rate', label: 'Recovery Rate', color: 'text-amber-400', bg: 'bg-amber-500/10', border: 'border-amber-500/20', suffix: '%' },
  { key: 'active_recoveries', label: 'Active Recoveries', color: 'text-blue-400', bg: 'bg-blue-500/10', border: 'border-blue-500/20' },
];

export default function StatCards({ stats }) {
  if (!stats) return null;
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      {cards.map((card) => (
        <div key={card.key} className={`${card.bg} ${card.border} border rounded-xl p-5 transition hover:scale-[1.02]`}>
          <p className="text-xs text-slate-400 font-medium uppercase tracking-wider">{card.label}</p>
          <p className={`text-2xl font-bold mt-2 font-mono ${card.color}`}>
            {card.isCurrency ? formatCurrency(stats[card.key]) : `${stats[card.key] ?? 0}${card.suffix || ''}`}
          </p>
        </div>
      ))}
    </div>
  );
}
