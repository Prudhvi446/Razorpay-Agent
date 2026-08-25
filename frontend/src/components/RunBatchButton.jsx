import React, { useState } from 'react';
import { runBatch } from '../api';

export default function RunBatchButton({ onComplete }) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  const handleClick = async () => {
    setLoading(true);
    setResult(null);
    try {
      const data = await runBatch();
      setResult(data);
      if (onComplete) onComplete();
    } catch (err) {
      setResult({ error: err.message || 'Failed to run batch' });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center gap-3">
      <button
        onClick={handleClick}
        disabled={loading}
        className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium bg-accent/20 text-accent border border-accent/30 hover:bg-accent/30 disabled:opacity-50 transition"
      >
        {loading ? (
          <>
            <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
            Running...
          </>
        ) : '▶ Run Agent Pipeline'}
      </button>
      {result && !result.error && (
        <span className="text-xs font-mono text-slate-400">
          ✓ Diagnosed: {result.diagnosed}, Executed: {result.actions_executed}, Promises: {result.promises_checked}
        </span>
      )}
      {result?.error && <span className="text-xs font-mono text-red-400">✗ {result.error}</span>}
    </div>
  );
}
