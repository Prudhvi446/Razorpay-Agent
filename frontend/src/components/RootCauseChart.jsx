import React from 'react';
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer, Legend } from 'recharts';

const COLORS = {
  soft_decline_retry: '#10b981',
  hard_decline_new_method: '#ef4444',
  network_bank_issue: '#f59e0b',
  auth_failure_3ds: '#3b82f6',
  mandate_issue: '#8b5cf6',
  customer_abandoned: '#ec4899',
  unrecoverable: '#6b7280',
};

const PRETTY = {
  soft_decline_retry: 'Soft Decline',
  hard_decline_new_method: 'Hard Decline',
  network_bank_issue: 'Network/Bank',
  auth_failure_3ds: '3DS Auth',
  mandate_issue: 'Mandate',
  customer_abandoned: 'Abandoned',
  unrecoverable: 'Unrecoverable',
};

const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const d = payload[0];
  return (
    <div className="bg-slate-900 border border-slate-700 px-3 py-2 rounded-lg shadow-xl text-xs">
      <p className="font-semibold text-white">{d.name}</p>
      <p className="text-emerald-400 font-mono mt-1">{d.value} cases</p>
    </div>
  );
};

export default function RootCauseChart({ data }) {
  if (!data || !data.length) return null;
  const chartData = data.map(d => ({ name: PRETTY[d.category] || d.category, value: d.count, key: d.category }));

  return (
    <ResponsiveContainer width="100%" height={280}>
      <PieChart>
        <Pie data={chartData} cx="50%" cy="50%" innerRadius={55} outerRadius={90} paddingAngle={3} dataKey="value" stroke="none">
          {chartData.map((d, i) => <Cell key={i} fill={COLORS[d.key] || '#6b7280'} />)}
        </Pie>
        <Tooltip content={<CustomTooltip />} />
        <Legend formatter={(v) => <span className="text-slate-300 text-xs">{v}</span>} />
      </PieChart>
    </ResponsiveContainer>
  );
}
