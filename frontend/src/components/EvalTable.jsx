import React, { useEffect, useState } from 'react';
import { getEval } from '../api';

export default function EvalTable() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getEval().then(setData).catch(console.error).finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="text-slate-400 text-sm">Loading evaluation...</p>;
  if (!data || !data.categories) return <p className="text-slate-500 text-sm">No evaluation data available. Run the seed script and agent pipeline first.</p>;

  const accColor = (acc) => acc >= 80 ? 'text-emerald-400' : acc >= 60 ? 'text-amber-400' : 'text-red-400';

  return (
    <div className="bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden">
      <div className="px-5 py-3 border-b border-slate-800">
        <h3 className="text-sm font-semibold text-white">Classification Accuracy vs Ground Truth</h3>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-slate-400 text-xs uppercase">
            <th className="text-left px-5 py-3">Category</th>
            <th className="text-right px-5 py-3">Total</th>
            <th className="text-right px-5 py-3">Correct</th>
            <th className="text-right px-5 py-3">Accuracy</th>
          </tr>
        </thead>
        <tbody>
          {data.categories.map((cat) => (
            <tr key={cat.category} className="border-b border-slate-800/50 hover:bg-slate-800/30">
              <td className="px-5 py-2.5 text-slate-200 font-mono text-xs">{cat.category}</td>
              <td className="px-5 py-2.5 text-right text-slate-300 font-mono">{cat.total}</td>
              <td className="px-5 py-2.5 text-right text-slate-300 font-mono">{cat.correct}</td>
              <td className={`px-5 py-2.5 text-right font-mono font-bold ${accColor(cat.accuracy)}`}>{cat.accuracy.toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="bg-slate-800/50">
            <td className="px-5 py-3 text-white font-semibold">Overall</td>
            <td className="px-5 py-3 text-right text-white font-mono">{data.overall_total}</td>
            <td className="px-5 py-3 text-right text-white font-mono">{data.overall_correct}</td>
            <td className={`px-5 py-3 text-right font-mono font-bold ${accColor(data.overall_accuracy)}`}>{data.overall_accuracy.toFixed(1)}%</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
