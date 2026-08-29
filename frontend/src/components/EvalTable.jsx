import React, { useEffect, useState } from 'react';
import { getEval } from '../api';

export default function EvalTable() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getEval().then(setData).catch(console.error).finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="text-slate-400 text-sm">Loading evaluation...</p>;
  if (!data || !data.categories) {
    return <p className="text-slate-500 text-sm">No evaluation data available. Run the seed script and agent pipeline first.</p>;
  }

  const accColor = (acc) => (acc >= 80 ? 'text-emerald-400' : acc >= 60 ? 'text-amber-400' : 'text-red-400');
  const ab = data.ab_comparison || {};
  const ctrl = ab.control_group || { total: 0, correct: 0, accuracy: 0 };
  const ai = ab.ai_group || { total: 0, correct: 0, accuracy: 0 };

  return (
    <div className="space-y-6">
      {/* A/B Group Comparison Cards */}
      {data.ab_comparison && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-slate-900/60 border border-slate-800 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Control Group</h4>
              <span className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">Static Baseline</span>
            </div>
            <div className="flex items-baseline justify-between mt-2">
              <div>
                <p className="text-2xl font-bold font-mono text-slate-300">{ctrl.accuracy}%</p>
                <p className="text-xs text-slate-500 mt-0.5">{ctrl.correct} of {ctrl.total} verified</p>
              </div>
              <span className="text-xs font-mono text-slate-400">Deterministic Rules</span>
            </div>
          </div>

          <div className="bg-emerald-950/20 border border-emerald-500/30 rounded-xl p-4">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-xs font-semibold text-emerald-400 uppercase tracking-wider">AI Recovery Group</h4>
              <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 font-mono">Agent + LLM</span>
            </div>
            <div className="flex items-baseline justify-between mt-2">
              <div>
                <p className="text-2xl font-bold font-mono text-emerald-400">{ai.accuracy}%</p>
                <p className="text-xs text-emerald-400/80 mt-0.5">{ai.correct} of {ai.total} verified</p>
              </div>
              <span className="text-xs font-mono text-emerald-300 font-semibold">Enriched Reasoning</span>
            </div>
          </div>
        </div>
      )}

      {/* Category Accuracy Table */}
      <div className="bg-slate-900/60 border border-slate-800 rounded-xl overflow-hidden">
        <div className="px-5 py-3 border-b border-slate-800 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-white">Root Cause Classification Accuracy vs Ground Truth</h3>
          <span className="text-xs text-slate-400 font-mono">
            Overall Accuracy: <span className={`font-bold ${accColor(data.overall_accuracy)}`}>{data.overall_accuracy}%</span>
          </span>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-slate-400 text-xs uppercase">
              <th className="text-left px-5 py-3">Root Cause Category</th>
              <th className="text-right px-5 py-3">Total Cases</th>
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
                <td className={`px-5 py-2.5 text-right font-mono font-bold ${accColor(cat.accuracy)}`}>
                  {cat.accuracy.toFixed(1)}%
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="bg-slate-800/50">
              <td className="px-5 py-3 text-white font-semibold">Total Verified</td>
              <td className="px-5 py-3 text-right text-white font-mono">{data.overall_total}</td>
              <td className="px-5 py-3 text-right text-white font-mono">{data.overall_correct}</td>
              <td className={`px-5 py-3 text-right font-mono font-bold ${accColor(data.overall_accuracy)}`}>
                {data.overall_accuracy.toFixed(1)}%
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  );
}

