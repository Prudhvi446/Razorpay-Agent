import axios from 'axios';

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000',
  timeout: 15000,
});

export const getStats = () => api.get('/api/stats').then(r => r.data);
export const getFunnel = () => api.get('/api/funnel').then(r => r.data);
export const getRootCauses = () => api.get('/api/root-causes').then(r => r.data);
export const getAuditLog = (limit = 50) => api.get(`/api/audit-log?limit=${limit}`).then(r => r.data);
export const runBatch = () => api.post('/api/run-batch').then(r => r.data);
export const getEval = () => api.get('/api/eval').then(r => r.data);

export default api;
