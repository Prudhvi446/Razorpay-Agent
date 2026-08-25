import React from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts';

const COLORS = ['#ef4444', '#f59e0b', '#3b82f6', '#10b981'];
const LABELS = ['Failed', 'Contacted', 'Promised', 'Paid'];

const CustomTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="bg-slate-900 border border-slate-700 px-3 py-2 rounded-lg shadow-xl text-xs">
      <p className="font-semibold text-white">{d.name}</p>
      <p className="text-emerald-400 font-mono mt-1">{d.value} payments</p>
    </div>
  );
};

export default function FunnelChart({ data }) {
  if (!data) return null;
  const chartData = LABELS.map((name, i) => ({
    name,
    value: [data.failed, data.contacted, data.promised, data.paid][i] || 0,
  }));

  return (
    <ResponsiveContainer width="100%" height={280}>
      <BarChart data={chartData} margin={{ top: 10, right: 10, left: -10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#334155" vertical={false} />
        <XAxis dataKey="name" stroke="#94a3b8" tick={{ fontSize: 12 }} />
        <YAxis stroke="#94a3b8" tick={{ fontSize: 12 }} />
        <Tooltip content={<CustomTooltip />} />
        <Bar dataKey="value" radius={[6, 6, 0, 0]} barSize={48}>
          {chartData.map((_, i) => <Cell key={i} fill={COLORS[i]} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
