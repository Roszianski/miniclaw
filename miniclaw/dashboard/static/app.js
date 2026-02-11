// miniclaw dashboard

function resolveDashboardToken() {
  let queryToken = '';
  let storedToken = '';
  try {
    const params = new URLSearchParams(window.location.search);
    queryToken = (params.get('token') || '').trim();
    storedToken = (sessionStorage.getItem('dashboard_token') || '').trim();
    if (queryToken) {
      sessionStorage.setItem('dashboard_token', queryToken);
      params.delete('token');
      const nextQuery = params.toString();
      const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ''}${window.location.hash}`;
      window.history.replaceState({}, '', nextUrl);
    }
  } catch (e) {
    // Ignore storage/history failures and fall back to query token only.
  }
  return queryToken || storedToken;
}

const TOKEN = resolveDashboardToken();
const headers = { 'Content-Type': 'application/json' };
if (TOKEN) {
  headers.Authorization = `Bearer ${TOKEN}`;
}

// --- Tailwind class constants ---
const TD = 'px-3 py-2.5 border-b border-gray-200 dark:border-gray-700 text-sm';
const TR = 'hover:bg-gray-50 dark:hover:bg-gray-800/30 transition-colors even:bg-gray-50/50 dark:even:bg-gray-800/20';
const BTN = 'px-3 py-1.5 text-sm rounded-lg border border-gray-200 dark:border-gray-700 bg-transparent text-gray-700 dark:text-gray-300 cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors focus-visible:ring-2 focus-visible:ring-blue-500/50 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-[#1a1d27] disabled:opacity-50 disabled:cursor-not-allowed';
const BTN_DANGER = 'px-3 py-1.5 text-sm rounded-lg border border-red-300 dark:border-red-500/50 text-red-500 cursor-pointer hover:bg-red-50 dark:hover:bg-red-500/10 transition-colors focus-visible:ring-2 focus-visible:ring-red-500/50 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-[#1a1d27] disabled:opacity-50 disabled:cursor-not-allowed';
const BTN_SUCCESS = 'px-3 py-1.5 text-sm rounded-lg border border-green-300 dark:border-green-500/50 text-green-600 dark:text-green-400 cursor-pointer hover:bg-green-50 dark:hover:bg-green-500/10 transition-colors focus-visible:ring-2 focus-visible:ring-green-500/50 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-[#1a1d27] disabled:opacity-50 disabled:cursor-not-allowed';

// --- Dirty state tracking ---
let currentPageId = 'dashboard';
let configDirty = false;
let memoryDirty = false;
let heartbeatDirty = false;
let workspaceDirty = false;

function hasDirtyState() {
  if (currentPageId === 'config' && configDirty) return true;
  if (currentPageId === 'memory' && memoryDirty) return true;
  if (currentPageId === 'heartbeat' && heartbeatDirty) return true;
  if (currentPageId === 'workspace' && workspaceDirty) return true;
  return false;
}

// --- Badge helpers ---
function badgeOk(text) {
  return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-green-100 dark:bg-green-500/15 text-green-600 dark:text-green-400">${text}</span>`;
}
function badgeErr(text) {
  return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-red-100 dark:bg-red-500/15 text-red-500">${text}</span>`;
}
function badgeDim(text) {
  return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-gray-100 dark:bg-gray-500/15 text-gray-500 dark:text-gray-400">${text}</span>`;
}

// --- Status card helper ---
function statusCard(label, value) {
  return `<div class="panel-card hover:shadow transition-shadow">
    <div class="text-xs text-gray-500 dark:text-gray-400 mb-1">${label}</div>
    <div class="text-sm font-medium">${value}</div>
  </div>`;
}

// --- Dashboard card icon SVGs ---
const STATUS_ICONS = {
  cpu: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 15h3M1 9h3M1 15h3"/></svg>',
  check: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
  shield: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  clock: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
  heart: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z"/></svg>',
  zap: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
  channel: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>',
  sessions: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>',
  skills: '<svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z"/></svg>',
  activity: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
};

// --- Color classes for dashboard cards ---
const COLOR_CLASSES = {
  green:  { border: 'border-l-green-500', bg: 'bg-green-50 dark:bg-green-500/10', text: 'text-green-600 dark:text-green-400' },
  gray:   { border: 'border-l-gray-300 dark:border-l-gray-600', bg: 'bg-gray-50 dark:bg-gray-500/10', text: 'text-gray-500 dark:text-gray-400' },
  blue:   { border: 'border-l-blue-500', bg: 'bg-blue-50 dark:bg-blue-500/10', text: 'text-blue-600 dark:text-blue-400' },
  yellow: { border: 'border-l-yellow-500', bg: 'bg-yellow-50 dark:bg-yellow-500/10', text: 'text-yellow-600 dark:text-yellow-400' },
  purple: { border: 'border-l-purple-500', bg: 'bg-purple-50 dark:bg-purple-500/10', text: 'text-purple-600 dark:text-purple-400' },
  rose:   { border: 'border-l-rose-500', bg: 'bg-rose-50 dark:bg-rose-500/10', text: 'text-rose-600 dark:text-rose-400' },
  orange: { border: 'border-l-orange-500', bg: 'bg-orange-50 dark:bg-orange-500/10', text: 'text-orange-600 dark:text-orange-400' },
};

// --- Status card metadata: maps labels to icon, color, and "on" value ---
const STATUS_CARD_META = {
  'Model':           { icon: 'cpu',     color: 'blue' },
  'Approvals':       { icon: 'check',   color: 'green',  onValue: 'on' },
  'Rate Limit':      { icon: 'shield',  color: 'yellow', onValue: 'on' },
  'Scheduled Tasks': { icon: 'clock',   color: 'purple' },
  'Heartbeat':       { icon: 'heart',   color: 'rose',   onValue: 'running' },
  'Active Runs':     { icon: 'zap',     color: 'orange' },
};

// --- Dashboard status card with icon + colored left border ---
function dashboardStatusCard(label, value) {
  const strVal = String(value);
  // Check for channel cards
  const isChannel = label.startsWith('Channel:');
  const meta = isChannel
    ? { icon: 'channel', color: 'blue', onValue: 'running' }
    : (STATUS_CARD_META[label] || { icon: 'cpu', color: 'blue' });

  const isOn = meta.onValue ? strVal.toLowerCase() === meta.onValue : true;
  const colorKey = isOn ? meta.color : 'gray';
  const c = COLOR_CLASSES[colorKey] || COLOR_CLASSES.gray;

  return `<div class="border border-gray-200 dark:border-gray-700 border-l-[3px] ${c.border} rounded-xl p-3 bg-white dark:bg-[#1a1d27] shadow-sm hover:shadow transition-shadow">
    <div class="flex items-center gap-1.5 mb-1">
      <span class="${c.text}">${STATUS_ICONS[meta.icon] || ''}</span>
      <span class="text-xs text-gray-500 dark:text-gray-400">${esc(label)}</span>
    </div>
    <div class="text-sm font-semibold">${esc(strVal)}</div>
  </div>`;
}

// --- Quick count card with tinted background ---
function quickCountCard(label, value, iconKey, colorKey) {
  const c = COLOR_CLASSES[colorKey] || COLOR_CLASSES.blue;
  const icon = STATUS_ICONS[iconKey] || '';
  return `<div class="${c.bg} rounded-xl p-3 transition-shadow hover:shadow">
    <div class="flex items-center gap-1.5 mb-1">
      <span class="${c.text}">${icon}</span>
      <span class="text-xs text-gray-500 dark:text-gray-400">${esc(label)}</span>
    </div>
    <div class="text-lg font-bold ${c.text}">${esc(String(value))}</div>
  </div>`;
}

// --- Toast notification system ---
const MAX_VISIBLE_TOASTS = 3;
const toastQueue = [];

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (container.children.length >= MAX_VISIBLE_TOASTS) {
    toastQueue.push({ message, type });
    return;
  }
  _renderToast(message, type);
}

function _renderToast(message, type) {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  const colors = {
    success: 'bg-green-50 dark:bg-green-500/15 border-green-200 dark:border-green-500/30 text-green-700 dark:text-green-400',
    error: 'bg-red-50 dark:bg-red-500/15 border-red-200 dark:border-red-500/30 text-red-700 dark:text-red-400',
    info: 'bg-blue-50 dark:bg-blue-500/15 border-blue-200 dark:border-blue-500/30 text-blue-700 dark:text-blue-400'
  };
  const icons = {
    success: '<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg>',
    error: '<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><circle cx="12" cy="17" r="0.5" fill="currentColor"/></svg>',
    info: '<svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/></svg>'
  };
  toast.className = `pointer-events-auto flex items-center gap-2 px-4 py-2.5 rounded-xl border text-sm shadow-lg toast-enter cursor-pointer ${colors[type] || colors.info}`;
  toast.innerHTML = `${icons[type] || icons.info}<span class="flex-1">${esc(message)}</span><button class="toast-close shrink-0 opacity-60 hover:opacity-100 transition-opacity ml-1 cursor-pointer" aria-label="Dismiss">&times;</button>`;
  container.appendChild(toast);
  function dismissToast() {
    if (toast.classList.contains('toast-exit')) return;
    toast.classList.remove('toast-enter');
    toast.classList.add('toast-exit');
    toast.addEventListener('animationend', () => {
      toast.remove();
      if (toastQueue.length) {
        const next = toastQueue.shift();
        _renderToast(next.message, next.type);
      }
    });
  }
  toast.addEventListener('click', dismissToast);
  setTimeout(dismissToast, 3000);
}

// --- Confirm dialog ---
function showConfirm(message) {
  return new Promise(resolve => {
    const modal = document.getElementById('confirm-modal');
    const msgEl = document.getElementById('confirm-message');
    const okBtn = document.getElementById('confirm-ok');
    const cancelBtn = document.getElementById('confirm-cancel');
    msgEl.textContent = message;
    modal.classList.remove('hidden', 'modal-exit');
    modal.classList.add('flex', 'modal-enter');
    cancelBtn.focus();
    function cleanup(result) {
      modal.classList.remove('modal-enter');
      modal.classList.add('modal-exit');
      modal.addEventListener('animationend', function hide() {
        modal.removeEventListener('animationend', hide);
        modal.classList.add('hidden');
        modal.classList.remove('flex', 'modal-exit');
        resolve(result);
      });
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onKey);
    }
    function onOk() { cleanup(true); }
    function onCancel() { cleanup(false); }
    function onKey(e) { if (e.key === 'Escape') cleanup(false); }
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    document.addEventListener('keydown', onKey);
  });
}

// --- Loading state helper ---
const SPINNER_SVG = '<svg class="btn-spinner w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" opacity="0.25"/><path d="M12 2a10 10 0 019.95 9" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>';

async function withLoading(btn, asyncFn) {
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.classList.add('opacity-60', 'cursor-not-allowed', 'pointer-events-none');
  btn.innerHTML = SPINNER_SVG + original;
  try {
    return await asyncFn();
  } finally {
    btn.disabled = false;
    btn.classList.remove('opacity-60', 'cursor-not-allowed', 'pointer-events-none');
    btn.innerHTML = original;
  }
}

// --- Empty state helper ---
function emptyState(icon, title, hint, action = null) {
  const icons = {
    sessions: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>',
    audit: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"/></svg>',
    skills: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z"/></svg>',
    cron: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>',
    approvals: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
    pointer: '<svg class="w-10 h-10" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122"/></svg>'
  };
  const actionHtml = action ? `<button class="${BTN} mt-3" onclick="${esc(action.onclick)}">${esc(action.label)}</button>` : '';
  return `<div class="flex flex-col items-center justify-center py-8 text-center">
    <div class="text-gray-300 dark:text-gray-600 mb-3">${icons[icon] || icons.pointer}</div>
    <div class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">${esc(title)}</div>
    <div class="text-xs text-gray-400 dark:text-gray-500">${esc(hint)}</div>
    ${actionHtml}
  </div>`;
}

// --- Skeleton loading helper ---
function skeleton(count = 3, type = 'lines') {
  if (type === 'cards') {
    return Array.from({ length: count }, () =>
      `<div class="skeleton-card"><div class="skeleton-line mb-2" style="width:60%"></div><div class="skeleton-line" style="width:40%"></div></div>`
    ).join('');
  }
  if (type === 'table') {
    return Array.from({ length: count }, () =>
      `<tr><td colspan="99" class="${TD}"><div class="skeleton-line" style="width:${60 + Math.random() * 30}%"></div></td></tr>`
    ).join('');
  }
  // lines
  const widths = ['100%', '85%', '70%', '90%', '60%'];
  return Array.from({ length: count }, (_, i) =>
    `<div class="skeleton-line mb-2" style="width:${widths[i % widths.length]}"></div>`
  ).join('');
}

// --- Relative time helpers ---
function relativeTime(date) {
  const diff = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diff < 5) return 'just now';
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return date.toLocaleString();
}

function relativeTimeFromNow(date) {
  const diff = Math.floor((date.getTime() - Date.now()) / 1000);
  if (diff < 0) return relativeTime(date);
  if (diff < 60) return `in ${diff}s`;
  if (diff < 3600) return `in ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `in ${Math.floor(diff / 3600)}h`;
  return `in ${Math.floor(diff / 86400)}d`;
}

function smartTimestamp(date) {
  const now = new Date();
  const isToday = date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  return isToday ? date.toLocaleTimeString() : date.toLocaleString();
}

// --- Friendly labels for technical values ---
const FRIENDLY_LABELS = {
  // Queue modes
  'queue': { label: 'One at a time', desc: 'Messages are processed sequentially in order' },
  'collect': { label: 'Batch messages', desc: 'Groups nearby messages into a single request' },
  'steer': { label: 'Load balance', desc: 'Routes messages to the least busy agent' },
  'followup': { label: 'Follow-up', desc: 'Queues messages as follow-ups to the current run' },
  'steer_backlog': { label: 'Load balance + backlog', desc: 'Load balances with overflow queue' },
  // Sandbox mode
  'off': { label: 'Disabled', desc: 'No sandboxing' },
  'non_main': { label: 'Sub-agents only', desc: 'Only sub-agents run in a sandbox' },
  'all': { label: 'All agents', desc: 'Every agent runs in a sandbox' },
  // Sandbox scope
  'session': { label: 'Per session', desc: 'Each session gets its own sandbox' },
  'agent': { label: 'Per agent', desc: 'Each agent gets its own sandbox' },
  'shared': { label: 'Shared', desc: 'All agents share one sandbox' },
  // Workspace access
  'none': { label: 'No access', desc: 'Sandbox cannot access the workspace' },
  'ro': { label: 'Read only', desc: 'Sandbox can read but not modify workspace files' },
  'rw': { label: 'Full access', desc: 'Sandbox can read and write workspace files' },
  // Approval profiles
  'coding': { label: 'Developer', desc: 'Balanced for coding tasks' },
  'messaging': { label: 'Messaging', desc: 'Optimized for chat and messaging' },
  'automation': { label: 'Automation', desc: 'Permissive for automated workflows' },
  'locked_down': { label: 'Maximum safety', desc: 'Asks permission for everything' },
  // Approval policies
  'always_allow': { label: 'Auto-approve', desc: 'Automatically allowed without asking' },
  'always_ask': { label: 'Ask every time', desc: 'Requires manual approval each time' },
  'always_deny': { label: 'Block', desc: 'Always denied automatically' },
  // Audit level
  'minimal': { label: 'Errors only', desc: 'Only logs errors and failures' },
  'standard': { label: 'Standard', desc: 'Logs important events and errors' },
  'verbose': { label: 'Everything', desc: 'Logs all activity in detail' },
  // Thinking
  'low': { label: 'Low', desc: 'Minimal reasoning' },
  'medium': { label: 'Medium', desc: 'Moderate reasoning' },
  'high': { label: 'High', desc: 'Deep reasoning' },
  // Auth mode
  'api_key': { label: 'API Key', desc: 'Authenticate with an API key' },
  'oauth': { label: 'OAuth', desc: 'Authenticate with OAuth tokens' },
};

// --- Field-level help descriptions ---
const FIELD_HELP = {
  'agents-defaults-model': 'Which AI model powers the agent. Changing this affects quality, speed, and cost.',
  'agents-defaults-workspace': 'Directory where the agent stores its identity and behavior files.',
  'agents-defaults-contextWindow': 'How much conversation history the AI can see at once. Higher = better context but more memory.',
  'agents-defaults-maxTokens': 'Maximum length of each AI response. Higher allows longer replies.',
  'agents-defaults-temperature': 'Controls how creative vs predictable responses are. Lower = more consistent, higher = more varied.',
  'agents-defaults-thinking': 'How much internal reasoning the AI does before responding. Higher = slower but more thoughtful.',
  'agents-defaults-supportsVision': 'Whether the AI can understand images sent in messages.',
  'agents-defaults-timeoutSeconds': 'How long to wait for an AI response before giving up.',
  'agents-defaults-streamEvents': 'Send real-time progress events while the AI is thinking.',
  'agents-defaults-queue-global': 'Use a single shared queue for all conversations instead of per-session queues.',
  'agents-defaults-queue-maxConcurrency': 'Maximum number of messages the AI can process simultaneously.',
  'agents-defaults-queue-mode': 'How incoming messages are processed when the AI is busy.',
  'agents-defaults-queue-collectWindowMs': 'How long to wait for additional messages before processing a batch (in milliseconds).',
  'agents-defaults-queue-maxBacklog': 'Maximum number of messages that can wait in the queue.',
  'channels-whatsapp-enabled': 'Turn the WhatsApp channel on or off.',
  'channels-whatsapp-bridgeUrl': 'WebSocket URL of the WhatsApp bridge service.',
  'channels-whatsapp-allowFrom': 'Phone numbers allowed to message the bot. Leave empty to allow all.',
  'channels-telegram-enabled': 'Turn the Telegram channel on or off.',
  'channels-telegram-token': 'Bot token from @BotFather on Telegram.',
  'channels-telegram-allowFrom': 'Telegram user IDs allowed to message the bot. Leave empty to allow all.',
  'channels-telegram-proxy': 'HTTP/SOCKS proxy URL for Telegram API access.',
  'gateway-host': 'Network address the API server listens on. Use 0.0.0.0 for all interfaces.',
  'gateway-port': 'Port number for the API server.',
  'sessions-idleResetMinutes': 'Automatically clear conversation history after this many minutes of inactivity. 0 = never.',
  'sessions-scheduledResetCron': 'Cron expression for when to reset all sessions (e.g. daily at 5 AM).',
  'audit-enabled': 'Log all agent activity for review in the Audit Log page.',
  'audit-level': 'How much detail to include in audit logs.',
  'rateLimit-enabled': 'Limit how fast the AI can send messages and use tools.',
  'rateLimit-messagesPerMinute': 'Maximum messages the AI can send per minute.',
  'rateLimit-toolCallsPerMinute': 'Maximum tool calls (commands, file edits, etc.) per minute.',
  'dashboard-enabled': 'Enable or disable this web dashboard.',
  'dashboard-port': 'Port number for the dashboard web server.',
  'dashboard-token': 'Secret token required to access the dashboard. Auto-generated if empty.',
  'transcription-localWhisper-enabled': 'Use a local Whisper model for voice message transcription.',
  'transcription-localWhisper-cli': 'Path to the whisper CLI executable.',
  'transcription-localWhisper-modelPath': 'Path to the Whisper model file.',
  'transcription-groqFallback': 'Fall back to Groq cloud API if local transcription fails.',
  'service-enabled': 'Run miniclaw as a background service.',
  'service-autoStart': 'Automatically start the service on system boot.',
  'hooks-enabled': 'Enable custom shell scripts that run in response to agent events.',
  'hooks-path': 'Directory containing hook scripts.',
  'hooks-configFile': 'Path to the hooks configuration file.',
  'hooks-timeoutSeconds': 'Maximum time a hook script can run before being killed.',
  'hooks-safeMode': 'Restrict hooks to only run pre-approved commands.',
  'hooks-allowCommandPrefixes': 'Command prefixes that hooks are allowed to execute.',
  'hooks-denyCommandPatterns': 'Command patterns that hooks are forbidden from executing.',
  'tools-web-search-apiKey': 'API key for the web search provider.',
  'tools-web-search-maxResults': 'Maximum number of search results to return.',
  'tools-exec-timeout': 'Maximum seconds a shell command can run.',
  'tools-exec-cpuSeconds': 'CPU time limit for commands (prevents runaway processes).',
  'tools-exec-memoryMb': 'Maximum memory a command can use.',
  'tools-exec-fileSizeMb': 'Maximum file size a command can create.',
  'tools-exec-maxProcesses': 'Maximum number of child processes a command can spawn.',
  'tools-sandbox-mode': 'Which agents run inside a Docker sandbox for isolation.',
  'tools-sandbox-scope': 'How sandboxes are shared between agents and sessions.',
  'tools-sandbox-workspaceAccess': 'Whether sandboxed agents can access workspace files.',
  'tools-sandbox-image': 'Docker image used for sandbox containers.',
  'tools-sandbox-pruneIdleSeconds': 'Remove idle sandbox containers after this many seconds.',
  'tools-sandbox-pruneMaxAgeSeconds': 'Remove sandbox containers older than this regardless of activity.',
  'tools-restrictToWorkspace': 'Prevent the AI from accessing files outside the workspace directory.',
  'tools-approvalProfile': 'Pre-configured set of approval rules. Controls what the AI can do without asking.',
  'tools-approval-exec': 'Whether running shell commands requires your approval.',
  'tools-approval-browser': 'Whether browsing the web requires your approval.',
  'tools-approval-webFetch': 'Whether fetching web pages requires your approval.',
  'tools-approval-writeFile': 'Whether writing or editing files requires your approval.',
};

function friendlyLabel(rawValue) {
  const entry = FRIENDLY_LABELS[rawValue];
  return entry ? `${entry.label} (${rawValue})` : rawValue;
}

function friendlySessionKey(key) {
  if (!key) return key;
  const parts = key.split(':');
  if (parts.length >= 2) {
    const channel = parts[0].charAt(0).toUpperCase() + parts[0].slice(1);
    return `${channel} - ${parts.slice(1).join(':')}`;
  }
  return key;
}

// --- Page name map ---
const PAGE_NAMES = {
  dashboard: 'Dashboard',
  chat: 'Chat',
  config: 'Configuration',
  sessions: 'Sessions',
  audit: 'Audit Log',
  skills: 'Skills',
  workspace: 'Workspace',
  memory: 'Memory',
  cron: 'Scheduled Tasks',
  heartbeat: 'Heartbeat',
  approvals: 'Approvals',
  status: 'Status'
};

// --- Page-level help panels ---
const PAGE_HELP = {
  chat: 'Talk directly to your AI agent in real time. Messages you send here go through the same pipeline as WhatsApp or Telegram messages.',
  config: 'All the settings that control how your AI agent behaves. Changes take effect after saving. Use the Form tab for guided editing or JSON for direct access.',
  sessions: 'Every conversation the AI has is stored as a session. You can review message history and monitor active processing runs here.',
  audit: 'A complete log of everything the AI has done -- tool calls, messages sent, commands run, and whether they succeeded or failed.',
  skills: 'Skills are plug-in capabilities you can add to the AI. Each skill teaches it how to do something new, like check the weather or manage GitHub issues.',
  workspace: 'These files define the AI\'s identity and behavior. SOUL.md is its personality, USER.md stores info about you, TOOLS.md describes available tools.',
  memory: 'The AI\'s long-term memory. MEMORY.md contains facts and preferences learned from conversations. You can edit or clear memories here.',
  cron: 'Set up tasks that run automatically on a schedule. Great for daily reports, periodic check-ins, or recurring reminders.',
  heartbeat: 'The heartbeat is a periodic self-check where the AI reviews its instructions and pending tasks. Configure how often it runs and what it checks.',
  approvals: 'When the AI needs to do something potentially risky -- like running a command or editing a file -- it asks for your permission here. You can approve or deny each action.',
  status: 'A live overview of your miniclaw instance -- which model is running, what channels are connected, and system health at a glance.',
};

function togglePageHelp(pageId) {
  const panel = document.getElementById('page-help-' + pageId);
  if (!panel) return;
  const isHidden = panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !isHidden);
  localStorage.setItem('page-help-' + pageId, isHidden ? 'open' : 'closed');
}

function injectPageHelp(pageId) {
  const helpText = PAGE_HELP[pageId];
  if (!helpText) return;
  const page = document.getElementById('page-' + pageId);
  if (!page) return;
  // Don't inject twice
  if (page.querySelector('.page-help-btn')) return;
  const subtitle = page.querySelector('p.text-sm');
  if (!subtitle) return;
  const savedState = localStorage.getItem('page-help-' + pageId);
  const isOpen = savedState === 'open';
  // Insert (?) button after the subtitle's parent header area
  const headerDiv = subtitle.parentElement;
  const btn = document.createElement('button');
  btn.className = 'page-help-btn inline-flex items-center justify-center w-5 h-5 rounded-full border border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500 text-xs font-semibold cursor-pointer hover:border-blue-400 hover:text-blue-500 dark:hover:border-blue-400 dark:hover:text-blue-400 transition-colors ml-1.5 align-middle';
  btn.setAttribute('aria-label', 'What is this page?');
  btn.textContent = '?';
  btn.onclick = () => togglePageHelp(pageId);
  subtitle.appendChild(btn);
  // Insert help panel
  const panel = document.createElement('div');
  panel.id = 'page-help-' + pageId;
  panel.className = `mt-2 px-3 py-2.5 rounded-lg bg-blue-50 dark:bg-blue-500/10 border border-blue-200 dark:border-blue-500/30 text-sm text-blue-700 dark:text-blue-300 leading-relaxed${isOpen ? '' : ' hidden'}`;
  panel.innerHTML = `<div class="flex items-start gap-2"><svg class="w-4 h-4 shrink-0 mt-0.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4m0-4h.01"/></svg><span>${esc(helpText)}</span></div>`;
  headerDiv.appendChild(panel);
}

// Navigation
async function showPage(id) {
  if (hasDirtyState()) {
    const confirmed = await showConfirm('You have unsaved changes. Leave without saving?');
    if (!confirmed) return;
  }
  doShowPage(id);
}

// --- Dirty state indicator ---
function updateDirtyIndicator(page, isDirty) {
  const link = document.querySelector(`[data-page="${page}"]`);
  if (link) {
    let dot = link.querySelector('.dirty-dot');
    if (!dot) {
      dot = document.createElement('span');
      dot.className = 'dirty-dot';
      link.appendChild(dot);
    }
    dot.classList.toggle('visible', isDirty);
  }
  // Toggle save button pulse
  const saveBtnMap = { config: 'btn-save-config', memory: 'btn-save-memory', heartbeat: 'btn-save-heartbeat', workspace: 'btn-save-workspace' };
  const saveBtn = saveBtnMap[page] ? document.getElementById(saveBtnMap[page]) : null;
  if (saveBtn) saveBtn.classList.toggle('save-dirty', isDirty);
}

function doShowPage(id) {
  currentPageId = id;
  configDirty = false;
  memoryDirty = false;
  heartbeatDirty = false;
  workspaceDirty = false;
  // Clear all dirty indicators on navigate
  ['config', 'memory', 'heartbeat', 'workspace'].forEach(p => updateDirtyIndicator(p, false));

  document.querySelectorAll('.page').forEach(p => {
    p.classList.add('hidden');
    p.classList.remove('flex', 'page-enter');
  });
  document.querySelectorAll('nav a[data-page]').forEach(a => a.classList.remove('active'));
  const page = document.getElementById('page-' + id);
  const link = document.querySelector(`[data-page="${id}"]`);
  if (page) {
    page.classList.remove('hidden');
    page.classList.add('flex', 'page-enter');
    page.addEventListener('animationend', () => page.classList.remove('page-enter'), { once: true });
  }
  if (link) link.classList.add('active');

  // Update breadcrumb
  const sep = document.getElementById('breadcrumb-sep');
  const crumb = document.getElementById('breadcrumb-page');
  if (id === 'dashboard') {
    sep.classList.add('hidden');
    crumb.classList.add('hidden');
  } else {
    sep.classList.remove('hidden');
    crumb.classList.remove('hidden');
    crumb.textContent = PAGE_NAMES[id] || id;
  }

  // Inject page help panel if applicable
  injectPageHelp(id);

  if (id === 'dashboard') loadDashboard();
  if (id === 'chat') initChat();
  if (id === 'config') loadConfig();
  if (id === 'sessions') loadSessions();
  if (id === 'audit') loadAudit();
  if (id === 'skills') loadSkills();
  if (id === 'workspace') loadWorkspace();
  if (id === 'memory') loadMemory();
  if (id === 'cron') loadCron();
  if (id === 'heartbeat') loadHeartbeat();
  if (id === 'approvals') initApprovals();
  if (id === 'status') loadStatus();
}

document.querySelectorAll('nav a[data-page]').forEach(a => {
  a.addEventListener('click', e => { e.preventDefault(); showPage(a.dataset.page); });
});

// === Collapsible Sidebar ===
function toggleSidebar() {
  if (window.innerWidth < 640) {
    // Mobile: slide sidebar in/out
    const sidebar = document.getElementById('main-sidebar');
    const isOpen = sidebar && sidebar.classList.contains('mobile-open');
    toggleSidebarMobile(!isOpen);
    return;
  }
  document.body.classList.toggle('sidebar-collapsed');
  localStorage.setItem('sidebar-collapsed', document.body.classList.contains('sidebar-collapsed'));
}

function initSidebar() {
  const saved = localStorage.getItem('sidebar-collapsed');
  if (saved === 'true') {
    document.body.classList.add('sidebar-collapsed');
  } else if (saved === null && window.innerWidth < 640) {
    document.body.classList.add('sidebar-collapsed');
  }
}

// === Dashboard ===
async function loadDashboard() {
  // Show skeletons while loading
  document.getElementById('dashboard-status-cards').innerHTML = skeleton(4, 'cards');
  try {
    const [statusRes, sessionsRes, skillsRes, cronRes, auditRes] = await Promise.all([
      fetch('/api/status', { headers }).then(r => r.json()).catch(() => ({})),
      fetch('/api/sessions', { headers }).then(r => r.json()).catch(() => []),
      fetch('/api/skills', { headers }).then(r => r.json()).catch(() => []),
      fetch('/api/cron', { headers }).then(r => r.json()).catch(() => []),
      fetch('/api/audit?limit=5', { headers }).then(r => r.json()).catch(() => [])
    ]);

    // Status cards (with icons + colored left border)
    const statusCards = document.getElementById('dashboard-status-cards');
    const parts = [];
    if (statusRes.model) parts.push(dashboardStatusCard('Model', statusRes.model));
    parts.push(dashboardStatusCard('Approvals', statusRes.approvals_enabled ? 'on' : 'off'));
    parts.push(dashboardStatusCard('Rate Limit', statusRes.rate_limit_enabled ? 'on' : 'off'));
    if (statusRes.cron) parts.push(dashboardStatusCard('Scheduled Tasks', statusRes.cron.jobs || 0));
    if (statusRes.heartbeat) {
      parts.push(dashboardStatusCard('Heartbeat', statusRes.heartbeat.running ? 'running' : 'stopped'));
    }
    if (statusRes.channels) {
      for (const [name, st] of Object.entries(statusRes.channels)) {
        parts.push(dashboardStatusCard(`Channel: ${name}`, st.running ? 'running' : 'stopped'));
      }
    }
    statusCards.innerHTML = parts.join('');

    // Quick counts (tinted backgrounds)
    const counts = document.getElementById('dashboard-counts');
    counts.innerHTML = [
      quickCountCard('Sessions', sessionsRes.length || 0, 'sessions', 'blue'),
      quickCountCard('Skills', skillsRes.length || 0, 'skills', 'purple'),
      quickCountCard('Scheduled Tasks', cronRes.length || 0, 'clock', 'yellow'),
      quickCountCard('Active Runs', (statusRes.runs && statusRes.runs.active) || 0, 'zap', 'orange')
    ].join('');

    // Recent audit
    const recent = document.getElementById('dashboard-recent');
    if (auditRes.length === 0) {
      recent.innerHTML = `<div class="flex flex-col items-center justify-center py-6 text-center">
        <div class="text-gray-300 dark:text-gray-600 mb-3">${STATUS_ICONS.activity}</div>
        <div class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-1">No recent activity</div>
        <div class="text-xs text-gray-400 dark:text-gray-500">Activity will appear here as the agent runs</div>
      </div>`;
    } else {
      recent.innerHTML = auditRes.reverse().map(e => {
        const time = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : '';
        const type = e.type || '';
        const detail = e.tool || e.event || e.dir || '';
        const ok = e.ok === false ? badgeErr('failed') : e.ok === true ? badgeOk('success') : '';
        return `<div class="flex items-center gap-2 py-1.5 border-b border-gray-100 dark:border-gray-800 last:border-0">
          <span class="text-xs text-gray-400 dark:text-gray-500 w-16 shrink-0">${time}</span>
          <span class="text-xs font-medium">${esc(type)}</span>
          <span class="text-xs text-gray-400 dark:text-gray-500 truncate flex-1">${esc(detail)}</span>
          ${ok}
        </div>`;
      }).join('');
    }
    // Quick navigation icon grid
    const quicknavGrid = document.getElementById('dashboard-quicknav');
    if (quicknavGrid) {
      const navItems = [
        { page: 'chat', label: 'Chat' },
        { page: 'skills', label: 'Skills' },
        { page: 'cron', label: 'Scheduled Tasks' },
        { page: 'config', label: 'Config' },
        { page: 'audit', label: 'Audit Log' },
        { page: 'memory', label: 'Memory' },
      ];
      quicknavGrid.innerHTML = navItems.map(({ page, label }) => {
        const link = document.querySelector(`nav a[data-page="${page}"] svg`);
        const iconSvg = link ? link.outerHTML.replace(/w-4 h-4/g, 'w-5 h-5') : '';
        return `<button onclick="showPage('${page}')" class="group flex flex-col items-center gap-2 p-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-[#1a1d27] hover:border-[#c96442] dark:hover:border-[#c96442] transition-all cursor-pointer">
          <span class="text-gray-400 dark:text-gray-500 group-hover:text-[#c96442] transition-colors">${iconSvg}</span>
          <span class="text-xs font-medium text-gray-600 dark:text-gray-400 group-hover:text-gray-900 dark:group-hover:text-gray-200 transition-colors">${esc(label)}</span>
        </button>`;
      }).join('');
    }
  } catch (e) {
    showToast('Failed to load dashboard. Check your connection.', 'error');
  }
}

// === Chat ===
let ws = null;

function updateChatStatus(state) {
  const el = document.getElementById('chat-status');
  if (!el) return;
  if (state === 'connected') {
    el.innerHTML = '<span class="inline-block w-2 h-2 rounded-full bg-green-500 mr-1.5"></span><span class="text-xs text-gray-500 dark:text-gray-400">Connected</span>';
  } else {
    el.innerHTML = '<span class="inline-block w-2 h-2 rounded-full bg-red-500 mr-1.5"></span><span class="text-xs text-gray-500 dark:text-gray-400">Disconnected</span>';
  }
}

function initChat() {
  if (ws && ws.readyState <= 1) return;
  updateChatStatus('disconnected');
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws/chat?token=${TOKEN}`);
  ws.onopen = () => { updateChatStatus('connected'); };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    appendChat(msg.role || 'assistant', msg.content || '');
  };
  ws.onerror = () => { showToast('Chat connection lost', 'error'); };
  ws.onclose = () => { ws = null; updateChatStatus('disconnected'); };
}

function appendChat(role, content) {
  const el = document.createElement('div');
  const base = 'max-w-[80%] px-4 py-3 rounded-2xl text-sm leading-relaxed whitespace-pre-wrap break-words';
  if (role === 'user') {
    el.className = `${base} self-end bg-blue-500 text-white`;
  } else {
    el.className = `${base} self-start bg-white dark:bg-[#1a1d27] border border-gray-200 dark:border-gray-700`;
  }
  el.textContent = content;
  const container = document.getElementById('chat-messages');
  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}

function sendChat() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || !ws) return;
  appendChat('user', text);
  ws.send(JSON.stringify({ content: text }));
  input.value = '';
}

document.getElementById('chat-input')?.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});

// === Config ===
let configData = {};
let configActiveTab = 'form';

// Provider names for accordion
const PROVIDER_NAMES = [
  'openai', 'anthropic', 'google', 'mistral', 'groq', 'together',
  'fireworks', 'deepseek', 'openrouter', 'ollama', 'custom'
];

// Field definitions: { id, path, type, label?, options?, ph? }
// ph = placeholder showing the Pydantic default so users see effective values
const CONFIG_FIELDS = [
  // Agent Defaults
  { id: 'agents-defaults-model', path: 'agents.defaults.model', type: 'text', label: 'Model', ph: 'anthropic/claude-opus-4-5' },
  { id: 'agents-defaults-workspace', path: 'agents.defaults.workspace', type: 'text', label: 'Workspace', ph: '~/.miniclaw/workspace' },
  { id: 'agents-defaults-contextWindow', path: 'agents.defaults.contextWindow', type: 'number', label: 'Context Window', ph: '32768' },
  { id: 'agents-defaults-maxTokens', path: 'agents.defaults.maxTokens', type: 'number', label: 'Max Tokens', ph: '8192' },
  { id: 'agents-defaults-temperature', path: 'agents.defaults.temperature', type: 'number', label: 'Temperature', step: '0.1', ph: '0.7' },
  { id: 'agents-defaults-thinking', path: 'agents.defaults.thinking', type: 'select', label: 'Thinking', options: ['off', 'low', 'medium', 'high'] },
  { id: 'agents-defaults-supportsVision', path: 'agents.defaults.supportsVision', type: 'checkbox', label: 'Supports Vision' },
  { id: 'agents-defaults-timeoutSeconds', path: 'agents.defaults.timeoutSeconds', type: 'number', label: 'Timeout (s)', ph: '180' },
  { id: 'agents-defaults-streamEvents', path: 'agents.defaults.streamEvents', type: 'checkbox', label: 'Stream Events' },
  { id: 'agents-defaults-queue-global', path: 'agents.defaults.queue.global', type: 'checkbox', label: 'Global Queue' },
  { id: 'agents-defaults-queue-maxConcurrency', path: 'agents.defaults.queue.maxConcurrency', type: 'number', label: 'Queue Max Concurrency', ph: '4' },
  { id: 'agents-defaults-queue-mode', path: 'agents.defaults.queue.mode', type: 'select', label: 'Queue Mode', options: ['queue', 'collect', 'steer', 'followup', 'steer_backlog'] },
  { id: 'agents-defaults-queue-collectWindowMs', path: 'agents.defaults.queue.collectWindowMs', type: 'number', label: 'Collect Window (ms)', ph: '1200' },
  { id: 'agents-defaults-queue-maxBacklog', path: 'agents.defaults.queue.maxBacklog', type: 'number', label: 'Max Backlog', ph: '8' },
  // WhatsApp
  { id: 'channels-whatsapp-enabled', path: 'channels.whatsapp.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'channels-whatsapp-bridgeUrl', path: 'channels.whatsapp.bridgeUrl', type: 'text', label: 'Bridge URL', ph: 'ws://localhost:3001' },
  { id: 'channels-whatsapp-allowFrom', path: 'channels.whatsapp.allowFrom', type: 'array', label: 'Allow From' },
  // Telegram
  { id: 'channels-telegram-enabled', path: 'channels.telegram.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'channels-telegram-token', path: 'channels.telegram.token', type: 'password', label: 'Token' },
  { id: 'channels-telegram-allowFrom', path: 'channels.telegram.allowFrom', type: 'array', label: 'Allow From' },
  { id: 'channels-telegram-proxy', path: 'channels.telegram.proxy', type: 'text', label: 'Proxy' },
  // Gateway
  { id: 'gateway-host', path: 'gateway.host', type: 'text', label: 'Host', ph: '0.0.0.0' },
  { id: 'gateway-port', path: 'gateway.port', type: 'number', label: 'Port' },
  // Sessions
  { id: 'sessions-idleResetMinutes', path: 'sessions.idleResetMinutes', type: 'number', label: 'Idle Reset (minutes)', ph: '0' },
  { id: 'sessions-scheduledResetCron', path: 'sessions.scheduledResetCron', type: 'text', label: 'Scheduled Reset Cron', ph: '0 5 * * *' },
  // Audit
  { id: 'audit-enabled', path: 'audit.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'audit-level', path: 'audit.level', type: 'select', label: 'Level', options: ['minimal', 'standard', 'verbose'] },
  // Rate Limit
  { id: 'rateLimit-enabled', path: 'rateLimit.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'rateLimit-messagesPerMinute', path: 'rateLimit.messagesPerMinute', type: 'number', label: 'Messages / min', ph: '20' },
  { id: 'rateLimit-toolCallsPerMinute', path: 'rateLimit.toolCallsPerMinute', type: 'number', label: 'Tool Calls / min', ph: '60' },
  // Dashboard
  { id: 'dashboard-enabled', path: 'dashboard.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'dashboard-port', path: 'dashboard.port', type: 'number', label: 'Port', ph: '18791' },
  { id: 'dashboard-token', path: 'dashboard.token', type: 'password', label: 'Token', ph: 'auto-generated' },
  // Transcription
  { id: 'transcription-localWhisper-enabled', path: 'transcription.localWhisper.enabled', type: 'checkbox', label: 'Local Whisper Enabled' },
  { id: 'transcription-localWhisper-cli', path: 'transcription.localWhisper.cli', type: 'text', label: 'Whisper CLI' },
  { id: 'transcription-localWhisper-modelPath', path: 'transcription.localWhisper.modelPath', type: 'text', label: 'Whisper Model Path' },
  { id: 'transcription-groqFallback', path: 'transcription.groqFallback', type: 'checkbox', label: 'Groq Fallback' },
  // Service
  { id: 'service-enabled', path: 'service.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'service-autoStart', path: 'service.autoStart', type: 'checkbox', label: 'Auto Start' },
  // Hooks
  { id: 'hooks-enabled', path: 'hooks.enabled', type: 'checkbox', label: 'Enabled' },
  { id: 'hooks-path', path: 'hooks.path', type: 'text', label: 'Path' },
  { id: 'hooks-configFile', path: 'hooks.configFile', type: 'text', label: 'Config File' },
  { id: 'hooks-timeoutSeconds', path: 'hooks.timeoutSeconds', type: 'number', label: 'Timeout (s)' },
  { id: 'hooks-safeMode', path: 'hooks.safeMode', type: 'checkbox', label: 'Safe Mode' },
  { id: 'hooks-allowCommandPrefixes', path: 'hooks.allowCommandPrefixes', type: 'array', label: 'Allow Command Prefixes' },
  { id: 'hooks-denyCommandPatterns', path: 'hooks.denyCommandPatterns', type: 'array', label: 'Deny Command Patterns' },
  // Tools - Web Search
  { id: 'tools-web-search-apiKey', path: 'tools.web.search.apiKey', type: 'password', label: 'API Key' },
  { id: 'tools-web-search-maxResults', path: 'tools.web.search.maxResults', type: 'number', label: 'Max Results', ph: '5' },
  // Tools - Exec
  { id: 'tools-exec-timeout', path: 'tools.exec.timeout', type: 'number', label: 'Timeout', ph: '60' },
  { id: 'tools-exec-cpuSeconds', path: 'tools.exec.resourceLimits.cpuSeconds', type: 'number', label: 'CPU Seconds', ph: '30' },
  { id: 'tools-exec-memoryMb', path: 'tools.exec.resourceLimits.memoryMb', type: 'number', label: 'Memory (MB)', ph: '512' },
  { id: 'tools-exec-fileSizeMb', path: 'tools.exec.resourceLimits.fileSizeMb', type: 'number', label: 'File Size (MB)', ph: '64' },
  { id: 'tools-exec-maxProcesses', path: 'tools.exec.resourceLimits.maxProcesses', type: 'number', label: 'Max Processes', ph: '64' },
  // Tools - Sandbox
  { id: 'tools-sandbox-mode', path: 'tools.sandbox.mode', type: 'select', label: 'Mode', options: ['off', 'non_main', 'all'] },
  { id: 'tools-sandbox-scope', path: 'tools.sandbox.scope', type: 'select', label: 'Scope', options: ['session', 'agent', 'shared'] },
  { id: 'tools-sandbox-workspaceAccess', path: 'tools.sandbox.workspaceAccess', type: 'select', label: 'Workspace Access', options: ['none', 'ro', 'rw'] },
  { id: 'tools-sandbox-image', path: 'tools.sandbox.image', type: 'text', label: 'Sandbox Image', ph: 'openclaw-sandbox:bookworm-slim' },
  { id: 'tools-sandbox-pruneIdleSeconds', path: 'tools.sandbox.pruneIdleSeconds', type: 'number', label: 'Prune Idle (s)', ph: '1800' },
  { id: 'tools-sandbox-pruneMaxAgeSeconds', path: 'tools.sandbox.pruneMaxAgeSeconds', type: 'number', label: 'Prune Max Age (s)', ph: '21600' },
  { id: 'tools-restrictToWorkspace', path: 'tools.restrictToWorkspace', type: 'checkbox', label: 'Restrict to Workspace' },
  { id: 'tools-approvalProfile', path: 'tools.approvalProfile', type: 'select', label: 'Approval Profile', options: ['coding', 'messaging', 'automation', 'locked_down'] },
  // Tools - Approval Policies
  { id: 'tools-approval-exec', path: 'tools.approval.exec', type: 'select', label: 'Exec', options: ['always_allow', 'always_ask', 'always_deny'] },
  { id: 'tools-approval-browser', path: 'tools.approval.browser', type: 'select', label: 'Browser', options: ['always_allow', 'always_ask', 'always_deny'] },
  { id: 'tools-approval-webFetch', path: 'tools.approval.webFetch', type: 'select', label: 'Web Fetch', options: ['always_allow', 'always_ask', 'always_deny'] },
  { id: 'tools-approval-writeFile', path: 'tools.approval.writeFile', type: 'select', label: 'Write/Edit File', options: ['always_allow', 'always_ask', 'always_deny'] },
];

// Dynamic provider fields (generated per provider)
function getProviderFields() {
  const fields = [];
  for (const name of PROVIDER_NAMES) {
    if (name === 'openai' || name === 'anthropic') {
      fields.push(
        { id: `providers-${name}-authMode`, path: `providers.${name}.authMode`, type: 'select', label: 'Auth Mode', options: ['api_key', 'oauth'] },
        { id: `providers-${name}-oauthTokenRef`, path: `providers.${name}.oauthTokenRef`, type: 'text', label: 'OAuth Token Ref', ph: `oauth:${name}:token` },
      );
    }
    fields.push(
      { id: `providers-${name}-apiKey`, path: `providers.${name}.apiKey`, type: 'password', label: 'API Key' },
      { id: `providers-${name}-apiBase`, path: `providers.${name}.apiBase`, type: 'text', label: 'API Base' },
      { id: `providers-${name}-extraHeaders`, path: `providers.${name}.extraHeaders`, type: 'json', label: 'Extra Headers' }
    );
  }
  return fields;
}

function getAllConfigFields() {
  return [...CONFIG_FIELDS, ...getProviderFields()];
}

// Nested object helpers
function getNestedValue(obj, path) {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : undefined), obj);
}

function setNestedValue(obj, path, value) {
  const keys = path.split('.');
  let cur = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    if (cur[keys[i]] === undefined || cur[keys[i]] === null || typeof cur[keys[i]] !== 'object') {
      cur[keys[i]] = {};
    }
    cur = cur[keys[i]];
  }
  cur[keys[keys.length - 1]] = value;
}

function configFormInput(field) {
  const inputCls = 'form-input w-full';
  const ph = field.ph ? ` placeholder="${esc(field.ph)}"` : '';
  const helpText = FIELD_HELP[field.id] || '';
  const helpHtml = helpText ? `<div class="text-xs text-gray-400 dark:text-gray-500 mt-0.5 mb-1 leading-relaxed">${esc(helpText)}</div>` : '';
  if (field.type === 'checkbox') {
    return `<label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <input type="checkbox" id="cf-${field.id}" class="rounded"> ${esc(field.label)}
    </label>${helpText ? `<div class="text-xs text-gray-400 dark:text-gray-500 ml-6 -mt-0.5 mb-1 leading-relaxed">${esc(helpText)}</div>` : ''}`;
  }
  const lbl = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-0.5" for="cf-${field.id}">${esc(field.label)}</label>${helpHtml}`;
  if (field.type === 'select') {
    const opts = (field.options || []).map(o => `<option value="${esc(o)}">${esc(friendlyLabel(o))}</option>`).join('');
    const hasPopover = INFO_POPOVER_CONTENT[field.id];
    const infoBtn = hasPopover ? `<button type="button" class="info-popover-btn inline-flex items-center justify-center w-5 h-5 rounded-full border border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500 text-xs font-bold cursor-pointer hover:border-blue-400 hover:text-blue-500 dark:hover:border-blue-400 dark:hover:text-blue-400 transition-colors ml-1.5 shrink-0" onclick="showInfoPopover('${field.id}', this.parentElement)" aria-label="More info">i</button>` : '';
    if (hasPopover) {
      return `${lbl}<div class="flex items-center gap-0"><select id="cf-${field.id}" class="${inputCls} flex-1">${opts}</select>${infoBtn}</div>`;
    }
    return `${lbl}<select id="cf-${field.id}" class="${inputCls}">${opts}</select>`;
  }
  if (field.type === 'password') {
    return `${lbl}<div class="password-wrapper"><input type="password" id="cf-${field.id}" class="${inputCls} pr-8" autocomplete="off"${ph}><button type="button" class="password-toggle" onclick="togglePasswordVisibility('cf-${field.id}')" aria-label="Toggle visibility"><svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button></div>`;
  }
  if (field.type === 'number') {
    const step = field.step ? ` step="${field.step}"` : '';
    return `${lbl}<input type="number" id="cf-${field.id}" class="${inputCls}"${step}${ph}>`;
  }
  if (field.type === 'array') {
    return `${lbl}<input type="text" id="cf-${field.id}" class="${inputCls}" placeholder="${field.ph ? esc(field.ph) : 'Comma-separated values'}">`;
  }
  if (field.type === 'json') {
    return `${lbl}<input type="text" id="cf-${field.id}" class="${inputCls}" placeholder='${field.ph ? esc(field.ph) : 'e.g. {"key":"value"}'}'>`;
  }
  return `${lbl}<input type="text" id="cf-${field.id}" class="${inputCls}"${ph}>`;
}

// --- Info popovers for complex fields ---
const INFO_POPOVER_CONTENT = {
  'agents-defaults-queue-mode': {
    title: 'Queue Mode',
    recommended: 'queue',
    getBody(val) {
      const descs = {
        'queue': 'Messages wait in line and are processed one at a time. Best for most use cases -- ensures orderly responses.',
        'collect': 'Waits briefly to group rapid messages together into one request. Good for users who send multiple short messages.',
        'steer': 'Routes each message to the least busy agent. Useful if running multiple agent instances.',
        'followup': 'New messages are queued as follow-ups to the current conversation turn.',
        'steer_backlog': 'Combines load balancing with an overflow queue for high-traffic scenarios.',
      };
      return descs[val] || 'Select a mode to see its description.';
    }
  },
  'tools-approvalProfile': {
    title: 'Approval Profile',
    recommended: 'coding',
    getBody(val) {
      const descs = {
        'coding': 'Balanced for development: auto-approves file edits and web access, asks before running shell commands.',
        'messaging': 'Conservative: blocks shell commands, asks before file writes. Good for chat-only bots.',
        'automation': 'Permissive: auto-approves everything. Only use in trusted, isolated environments.',
        'locked_down': 'Maximum safety: asks permission for every tool. Best when you want full control over all actions.',
      };
      return descs[val] || 'Select a profile to see its description.';
    }
  },
  'tools-sandbox-mode': {
    title: 'Sandbox Mode',
    recommended: 'off',
    getBody(val) {
      const descs = {
        'off': 'No sandboxing. Commands run directly on the host. Simple but less isolated.',
        'non_main': 'Only sub-agents run inside Docker containers. The main agent runs on the host.',
        'all': 'Every agent runs inside a Docker sandbox. Maximum isolation but requires Docker.',
      };
      return descs[val] || 'Select a mode to see its description.';
    }
  },
};

function showInfoPopover(fieldId, anchorEl) {
  closeInfoPopover();
  const config = INFO_POPOVER_CONTENT[fieldId];
  if (!config) return;
  const selectEl = document.getElementById('cf-' + fieldId);
  const currentVal = selectEl ? selectEl.value : '';
  const body = config.getBody(currentVal);
  const isRecommended = currentVal === config.recommended;
  const recBadge = isRecommended ? '<span class="inline-block px-1.5 py-0.5 rounded text-xs font-medium bg-green-100 dark:bg-green-500/15 text-green-600 dark:text-green-400 ml-2">Recommended</span>' : '';
  const popover = document.createElement('div');
  popover.id = 'info-popover';
  popover.className = 'info-popover';
  popover.innerHTML = `<div class="flex items-center justify-between mb-1.5"><span class="text-xs font-semibold text-gray-700 dark:text-gray-200">${esc(config.title)}</span>${recBadge}<button onclick="closeInfoPopover()" class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 text-sm cursor-pointer ml-2">&times;</button></div><div class="text-xs text-gray-600 dark:text-gray-400 leading-relaxed">${esc(body)}</div>`;
  anchorEl.style.position = 'relative';
  anchorEl.appendChild(popover);
  // Close on outside click
  setTimeout(() => document.addEventListener('click', closeInfoPopoverOnOutside), 0);
}

function closeInfoPopover() {
  const existing = document.getElementById('info-popover');
  if (existing) existing.remove();
  document.removeEventListener('click', closeInfoPopoverOnOutside);
}

function closeInfoPopoverOnOutside(e) {
  const popover = document.getElementById('info-popover');
  if (popover && !popover.contains(e.target) && !e.target.classList.contains('info-popover-btn')) {
    closeInfoPopover();
  }
}

// --- Approval profile comparison matrix ---
const APPROVAL_MATRIX = {
  coding:      { exec: 'always_ask',  browser: 'always_allow', webFetch: 'always_allow', writeFile: 'always_allow' },
  messaging:   { exec: 'always_deny', browser: 'always_allow', webFetch: 'always_allow', writeFile: 'always_ask' },
  automation:  { exec: 'always_allow', browser: 'always_allow', webFetch: 'always_allow', writeFile: 'always_allow' },
  locked_down: { exec: 'always_ask',  browser: 'always_ask',   webFetch: 'always_ask',   writeFile: 'always_ask' },
};

const APPROVAL_CELL_DISPLAY = {
  'always_allow': { label: 'Auto', cls: 'bg-green-100 dark:bg-green-500/15 text-green-600 dark:text-green-400' },
  'always_ask':   { label: 'Ask',  cls: 'bg-yellow-100 dark:bg-yellow-500/15 text-yellow-600 dark:text-yellow-400' },
  'always_deny':  { label: 'Block', cls: 'bg-red-100 dark:bg-red-500/15 text-red-500' },
};

function renderApprovalMatrix(currentProfile) {
  const profiles = Object.keys(APPROVAL_MATRIX);
  const tools = ['exec', 'browser', 'webFetch', 'writeFile'];
  const toolLabels = { exec: 'Shell Commands', browser: 'Browser', webFetch: 'Web Fetch', writeFile: 'Write/Edit Files' };
  const headerCells = profiles.map(p => {
    const fl = FRIENDLY_LABELS[p];
    const label = fl ? fl.label : p;
    const isActive = p === currentProfile;
    return `<th class="px-2 py-1.5 text-xs font-medium text-center ${isActive ? 'text-blue-600 dark:text-blue-400 bg-blue-50 dark:bg-blue-500/10' : 'text-gray-500 dark:text-gray-400'}">${esc(label)}</th>`;
  }).join('');
  const bodyRows = tools.map(t => {
    const cells = profiles.map(p => {
      const policy = APPROVAL_MATRIX[p][t];
      const display = APPROVAL_CELL_DISPLAY[policy] || { label: policy, cls: '' };
      const isActive = p === currentProfile;
      return `<td class="px-2 py-1.5 text-center ${isActive ? 'bg-blue-50/50 dark:bg-blue-500/5' : ''}"><span class="inline-block px-1.5 py-0.5 rounded text-xs font-medium ${display.cls}">${display.label}</span></td>`;
    }).join('');
    return `<tr class="border-b border-gray-100 dark:border-gray-700/50 last:border-0"><td class="px-2 py-1.5 text-xs text-gray-600 dark:text-gray-400">${toolLabels[t]}</td>${cells}</tr>`;
  }).join('');
  return `<div class="mt-2 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
    <table class="w-full text-sm"><thead><tr class="border-b border-gray-200 dark:border-gray-700"><th class="px-2 py-1.5 text-xs text-left text-gray-500 dark:text-gray-400">Tool</th>${headerCells}</tr></thead><tbody>${bodyRows}</tbody></table>
  </div>`;
}

// --- Config section descriptions ---
const CONFIG_SECTION_HELP = {
  'Agent Defaults': 'Core AI behavior -- which model to use, how creative it is, and how responses are queued.',
  'WhatsApp': 'Connect the bot to WhatsApp via a bridge service.',
  'Telegram': 'Connect the bot to Telegram using a bot token.',
  'Providers': 'API keys and endpoints for AI model providers.',
  'Gateway': 'Network settings for the API server that other services connect to.',
  'Sessions': 'How conversation history is managed and automatically cleared.',
  'Tools': 'What the AI is allowed to do -- run commands, browse web, edit files, and safety settings.',
  'Audit': 'Activity logging for reviewing what the agent has done.',
  'Rate Limit': 'Prevent the AI from sending too many messages or using too many tools per minute.',
  'Dashboard': 'Settings for this web dashboard interface.',
  'Service': 'Run miniclaw as a persistent background service.',
  'Hooks': 'Custom scripts that run automatically in response to agent events.',
};

let configGroupFieldMap = {};

function configCard(title, fieldsHtml, fieldIds, opts = {}) {
  const slug = title.replace(/\s+/g, '-').toLowerCase();
  configGroupFieldMap[slug] = fieldIds || [];
  const expanded = opts.expanded || false;
  const sectionHelp = CONFIG_SECTION_HELP[title] || '';
  const subtitleHtml = sectionHelp ? `<div class="text-xs font-normal normal-case tracking-normal text-gray-400 dark:text-gray-500 mt-0.5">${esc(sectionHelp)}</div>` : '';
  const chevronSvg = `<svg class="w-4 h-4 accordion-chevron shrink-0${expanded ? ' rotate-180' : ''}" id="config-chevron-${slug}" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 9l-7 7-7-7"/></svg>`;
  return `<div class="panel-card">
    <button type="button" class="w-full flex items-start justify-between text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider cursor-pointer hover:text-gray-700 dark:hover:text-gray-300 transition-colors text-left" onclick="toggleConfigGroup('${slug}')">
      <div>
        <span>${esc(title)} <span id="config-count-${slug}" class="normal-case text-gray-400 dark:text-gray-500 font-normal"></span></span>
        ${subtitleHtml}
      </div>
      ${chevronSvg}
    </button>
    <div id="config-group-body-${slug}" class="accordion-body${expanded ? ' open' : ''}" ${expanded ? 'style="max-height:2000px"' : ''}>
      <div class="space-y-2 pt-2">${fieldsHtml}</div>
    </div>
  </div>`;
}

function buildConfigForm() {
  configGroupFieldMap = {};
  const container = document.getElementById('config-form-tab');
  const cards = [];

  // Helper to render a set of fields
  function renderFields(ids) {
    const allFields = getAllConfigFields();
    return ids.map(id => {
      const f = allFields.find(x => x.id === id);
      return f ? configFormInput(f) : '';
    }).join('');
  }

  // Agent Defaults
  const agentDefaultIds = [
    'agents-defaults-model', 'agents-defaults-workspace',
    'agents-defaults-contextWindow', 'agents-defaults-maxTokens',
    'agents-defaults-temperature', 'agents-defaults-thinking',
    'agents-defaults-supportsVision', 'agents-defaults-timeoutSeconds',
    'agents-defaults-streamEvents',
    'agents-defaults-queue-global', 'agents-defaults-queue-maxConcurrency', 'agents-defaults-queue-mode',
    'agents-defaults-queue-collectWindowMs', 'agents-defaults-queue-maxBacklog'
  ];
  cards.push(configCard('Agent Defaults', renderFields(agentDefaultIds), agentDefaultIds, {}));

  // WhatsApp
  const whatsappIds = ['channels-whatsapp-enabled', 'channels-whatsapp-bridgeUrl', 'channels-whatsapp-allowFrom'];
  cards.push(configCard('WhatsApp', renderFields(whatsappIds), whatsappIds));

  // Telegram
  const telegramIds = ['channels-telegram-enabled', 'channels-telegram-token', 'channels-telegram-allowFrom', 'channels-telegram-proxy'];
  cards.push(configCard('Telegram', renderFields(telegramIds), telegramIds));

  // Providers (full width with accordions)
  let provHtml = '';
  const allProviderFieldIds = [];
  for (const name of PROVIDER_NAMES) {
    const chevron = `<svg class="w-4 h-4 accordion-chevron" id="chevron-${name}" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M19 9l-7 7-7-7"/></svg>`;
    const providerFieldIds = [
      `providers-${name}-apiKey`, `providers-${name}-apiBase`, `providers-${name}-extraHeaders`
    ];
    if (name === 'openai' || name === 'anthropic') {
      providerFieldIds.unshift(`providers-${name}-oauthTokenRef`);
      providerFieldIds.unshift(`providers-${name}-authMode`);
    }
    allProviderFieldIds.push(...providerFieldIds);
    const bodyFields = renderFields(providerFieldIds);
    provHtml += `<div class="border border-gray-100 dark:border-gray-700/50 rounded-lg overflow-hidden">
      <button type="button" class="w-full flex items-center justify-between px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/30 transition-colors cursor-pointer" onclick="toggleProviderAccordion('${name}')">
        <span>${esc(name.charAt(0).toUpperCase() + name.slice(1))}</span>
        ${chevron}
      </button>
      <div id="provider-body-${name}" class="accordion-body px-3 space-y-2">${bodyFields}</div>
    </div>`;
  }
  cards.push(configCard('Providers', `<div class="space-y-1">${provHtml}</div>`, allProviderFieldIds, {}));

  // Gateway
  const gatewayIds = ['gateway-host', 'gateway-port'];
  cards.push(configCard('Gateway', renderFields(gatewayIds), gatewayIds));

  // Sessions
  const sessionIds = ['sessions-idleResetMinutes', 'sessions-scheduledResetCron'];
  cards.push(configCard('Sessions', renderFields(sessionIds), sessionIds));

  // Tools (full width)
  const toolsFieldIds = [
    'tools-web-search-apiKey', 'tools-web-search-maxResults',
    'tools-exec-timeout', 'tools-exec-cpuSeconds', 'tools-exec-memoryMb', 'tools-exec-fileSizeMb', 'tools-exec-maxProcesses',
    'tools-sandbox-mode', 'tools-sandbox-scope', 'tools-sandbox-workspaceAccess', 'tools-sandbox-image',
    'tools-sandbox-pruneIdleSeconds', 'tools-sandbox-pruneMaxAgeSeconds', 'tools-restrictToWorkspace',
    'tools-approvalProfile',
    'tools-approval-exec', 'tools-approval-browser', 'tools-approval-webFetch', 'tools-approval-writeFile'
  ];
  const toolsHtml = `
    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">Web Search</div>
    ${renderFields(['tools-web-search-apiKey', 'tools-web-search-maxResults'])}
    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 mt-3 mb-1">Exec</div>
    ${renderFields(['tools-exec-timeout', 'tools-exec-cpuSeconds', 'tools-exec-memoryMb', 'tools-exec-fileSizeMb', 'tools-exec-maxProcesses'])}
    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 mt-3 mb-1">Sandbox</div>
    ${renderFields([
      'tools-sandbox-mode',
      'tools-sandbox-scope',
      'tools-sandbox-workspaceAccess',
      'tools-sandbox-image',
      'tools-sandbox-pruneIdleSeconds',
      'tools-sandbox-pruneMaxAgeSeconds',
      'tools-restrictToWorkspace'
    ])}
    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 mt-3 mb-1">Approval Profile</div>
    ${renderFields(['tools-approvalProfile'])}
    <div id="approval-matrix-container"></div>
    <div class="text-xs font-medium text-gray-500 dark:text-gray-400 mt-3 mb-1">Approval Policies</div>
    ${renderFields(['tools-approval-exec', 'tools-approval-browser', 'tools-approval-webFetch', 'tools-approval-writeFile'])}
  `;
  cards.push(configCard('Tools', toolsHtml, toolsFieldIds, {}));

  // Audit
  const auditIds = ['audit-enabled', 'audit-level'];
  cards.push(configCard('Audit', renderFields(auditIds), auditIds));

  // Rate Limit
  const rateLimitIds = ['rateLimit-enabled', 'rateLimit-messagesPerMinute', 'rateLimit-toolCallsPerMinute'];
  cards.push(configCard('Rate Limit', renderFields(rateLimitIds), rateLimitIds));

  // Dashboard
  const dashboardIds = ['dashboard-enabled', 'dashboard-port', 'dashboard-token'];
  cards.push(configCard('Dashboard', renderFields(dashboardIds), dashboardIds));

  // Service
  const serviceIds = ['service-enabled', 'service-autoStart'];
  cards.push(configCard('Service', renderFields(serviceIds), serviceIds));

  // Hooks
  const hooksIds = [
    'hooks-enabled', 'hooks-path', 'hooks-configFile', 'hooks-timeoutSeconds',
    'hooks-safeMode', 'hooks-allowCommandPrefixes', 'hooks-denyCommandPatterns'
  ];
  cards.push(configCard('Hooks', renderFields(hooksIds), hooksIds));

  container.innerHTML = cards.join('');
}

function jsonToForm(obj) {
  const allFields = getAllConfigFields();
  for (const field of allFields) {
    const el = document.getElementById('cf-' + field.id);
    if (!el) continue;
    const val = getNestedValue(obj, field.path);
    if (field.type === 'checkbox') {
      el.checked = !!val;
    } else if (field.type === 'array') {
      el.value = Array.isArray(val) ? val.join(', ') : (val || '');
    } else if (field.type === 'json') {
      el.value = val && typeof val === 'object' ? JSON.stringify(val) : (val || '');
    } else if (field.type === 'select') {
      el.value = val || '';
    } else if (field.type === 'number') {
      el.value = val !== undefined && val !== null ? val : '';
    } else {
      el.value = val !== undefined && val !== null ? val : '';
    }
  }
}

function formToJson() {
  const allFields = getAllConfigFields();
  for (const field of allFields) {
    const el = document.getElementById('cf-' + field.id);
    if (!el) continue;
    let val;
    if (field.type === 'checkbox') {
      val = el.checked;
    } else if (field.type === 'array') {
      const raw = el.value.trim();
      val = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
    } else if (field.type === 'json') {
      const raw = el.value.trim();
      if (!raw) {
        val = null;
      } else {
        try { val = JSON.parse(raw); } catch { val = raw; }
      }
    } else if (field.type === 'number') {
      const raw = el.value.trim();
      val = raw === '' ? undefined : Number(raw);
    } else if (field.type === 'select') {
      val = el.value || undefined;
    } else {
      // text, password — empty string → null for nullable fields
      const raw = el.value.trim();
      val = raw || null;
    }
    if (val !== undefined) {
      // Only write if the field already exists in config or has a non-default value.
      // This prevents bloating the config with null/false/[] for fields the user never set.
      const existing = getNestedValue(configData, field.path);
      const isDefault = val === null || val === false || (Array.isArray(val) && val.length === 0);
      if (existing !== undefined || !isDefault) {
        setNestedValue(configData, field.path, val);
      }
    }
  }
  document.getElementById('config-editor').value = JSON.stringify(configData, null, 2);
}

function switchConfigTab(tab) {
  configActiveTab = tab;
  const formTab = document.getElementById('config-form-tab');
  const jsonTab = document.getElementById('config-json-tab');
  const btnForm = document.getElementById('config-tab-form');
  const btnJson = document.getElementById('config-tab-json');

  if (tab === 'form') {
    // Sync JSON → form
    try {
      configData = JSON.parse(document.getElementById('config-editor').value);
    } catch {}
    jsonToForm(configData);
    formTab.classList.remove('hidden');
    jsonTab.classList.add('hidden');
    btnForm.classList.add('active');
    btnJson.classList.remove('active');
  } else {
    // Sync form → JSON
    formToJson();
    formTab.classList.add('hidden');
    jsonTab.classList.remove('hidden');
    btnForm.classList.remove('active');
    btnJson.classList.add('active');
  }
}

function toggleProviderAccordion(name) {
  const body = document.getElementById('provider-body-' + name);
  const chevron = document.getElementById('chevron-' + name);
  if (!body) return;
  const isOpen = body.classList.contains('open');
  if (isOpen) {
    body.style.maxHeight = '0';
    body.classList.remove('open');
  } else {
    body.classList.add('open');
    // Measure after class is applied so padding is included in scrollHeight
    requestAnimationFrame(() => {
      body.style.maxHeight = body.scrollHeight + 'px';
    });
  }
  if (chevron) chevron.classList.toggle('rotate-180');
}

function toggleConfigGroup(slug) {
  const body = document.getElementById('config-group-body-' + slug);
  const chevron = document.getElementById('config-chevron-' + slug);
  if (!body) return;
  const isOpen = body.classList.contains('open');
  if (isOpen) {
    body.style.maxHeight = '0';
    body.classList.remove('open');
  } else {
    body.classList.add('open');
    requestAnimationFrame(() => {
      body.style.maxHeight = body.scrollHeight + 'px';
    });
  }
  if (chevron) chevron.classList.toggle('rotate-180');
}

function updateConfigCounts() {
  const allFields = getAllConfigFields();
  for (const [slug, fieldIds] of Object.entries(configGroupFieldMap)) {
    const el = document.getElementById('config-count-' + slug);
    if (!el || !fieldIds.length) continue;
    let configured = 0;
    for (const id of fieldIds) {
      const input = document.getElementById('cf-' + id);
      if (!input) continue;
      const field = allFields.find(f => f.id === id);
      if (!field) continue;
      if (field.type === 'checkbox') {
        if (input.checked) configured++;
      } else {
        if (input.value && input.value.trim()) configured++;
      }
    }
    el.textContent = `(${configured}/${fieldIds.length})`;
  }
}

function togglePasswordVisibility(inputId) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.type = input.type === 'password' ? 'text' : 'password';
}

let configFormBuilt = false;

async function loadConfig() {
  try {
    const res = await fetch('/api/config', { headers });
    configData = await res.json();
    document.getElementById('config-editor').value = JSON.stringify(configData, null, 2);
    if (!configFormBuilt) {
      buildConfigForm();
      configFormBuilt = true;
      // Event delegation for dirty tracking on form tab
      document.getElementById('config-form-tab').addEventListener('input', () => { configDirty = true; updateDirtyIndicator('config', true); });
      document.getElementById('config-form-tab').addEventListener('change', () => { configDirty = true; updateDirtyIndicator('config', true); });
      // Approval matrix: re-render on profile change
      const profileSelect = document.getElementById('cf-tools-approvalProfile');
      if (profileSelect) {
        profileSelect.addEventListener('change', () => {
          const container = document.getElementById('approval-matrix-container');
          if (container) container.innerHTML = renderApprovalMatrix(profileSelect.value);
        });
      }
      // Live-update info popovers on dropdown change
      for (const fieldId of Object.keys(INFO_POPOVER_CONTENT)) {
        const sel = document.getElementById('cf-' + fieldId);
        if (sel) {
          sel.addEventListener('change', () => {
            const popover = document.getElementById('info-popover');
            if (popover) {
              // Re-show with updated value
              const btn = popover.parentElement;
              closeInfoPopover();
              if (btn) showInfoPopover(fieldId, btn);
            }
          });
        }
      }
    }
    jsonToForm(configData);
    updateConfigCounts();
    // Render approval matrix with current profile value
    const matrixContainer = document.getElementById('approval-matrix-container');
    const profileEl = document.getElementById('cf-tools-approvalProfile');
    if (matrixContainer && profileEl) {
      matrixContainer.innerHTML = renderApprovalMatrix(profileEl.value || 'coding');
    }
    configDirty = false;
    hideConfigBanner();
    // Ensure correct tab is shown
    switchConfigTab(configActiveTab);
  } catch (e) {
    showToast('Failed to load configuration. Check your connection.', 'error');
  }
}

function hideConfigBanner() {
  const banner = document.getElementById('config-banner');
  banner.classList.add('hidden');
  banner.classList.remove('flex');
}

function showConfigBanner(type, message) {
  const banner = document.getElementById('config-banner');
  banner.classList.remove('hidden', 'bg-red-50', 'dark:bg-red-500/10', 'border-red-200', 'dark:border-red-500/30', 'text-red-700', 'dark:text-red-400',
    'bg-green-50', 'dark:bg-green-500/10', 'border-green-200', 'dark:border-green-500/30', 'text-green-700', 'dark:text-green-400');
  banner.classList.add('flex');
  if (type === 'error') {
    banner.classList.add('bg-red-50', 'dark:bg-red-500/10', 'border', 'border-red-200', 'dark:border-red-500/30', 'text-red-700', 'dark:text-red-400');
    banner.innerHTML = `<svg class="w-4 h-4 shrink-0 mt-0.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg><div class="whitespace-pre-wrap">${esc(message)}</div>`;
  } else {
    banner.classList.add('bg-green-50', 'dark:bg-green-500/10', 'border', 'border-green-200', 'dark:border-green-500/30', 'text-green-700', 'dark:text-green-400');
    banner.innerHTML = `<svg class="w-4 h-4 shrink-0 mt-0.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M5 13l4 4L19 7"/></svg><div>${esc(message)}</div>`;
    setTimeout(hideConfigBanner, 3000);
  }
}

async function saveConfig() {
  const btn = document.getElementById('btn-save-config');
  await withLoading(btn, async () => {
    try {
      // Sync form → JSON if on form tab
      if (configActiveTab === 'form') formToJson();
      const ok = await doValidateConfig();
      if (!ok) return;
      const text = document.getElementById('config-editor').value;
      const body = JSON.parse(text);
      await fetch('/api/config', { method: 'PUT', headers, body: JSON.stringify(body) });
      configData = body;
      configDirty = false;
      updateDirtyIndicator('config', false);
      showToast('Config saved', 'success');
    } catch (e) {
      showToast('Action failed. Please try again.', 'error');
    }
  });
}

async function validateConfig() {
  const btn = document.getElementById('btn-validate');
  await withLoading(btn, async () => {
    // Sync form → JSON if on form tab
    if (configActiveTab === 'form') formToJson();
    const ok = await doValidateConfig();
    if (ok) showConfigBanner('success', 'Configuration is valid');
  });
}

async function doValidateConfig() {
  hideConfigBanner();
  const text = document.getElementById('config-editor').value;
  try {
    const body = JSON.parse(text);
    const res = await fetch('/api/config/validate', { method: 'POST', headers, body: JSON.stringify(body) });
    const data = await res.json();
    if (!data.ok) {
      const msg = (data.errors || []).join('\n') || 'Validation failed';
      showConfigBanner('error', msg);
      return false;
    }
    return true;
  } catch (e) {
    showConfigBanner('error', 'Invalid JSON: ' + e.message);
    return false;
  }
}

// === Sessions ===
async function loadSessions() {
  // Show detail empty state
  const detail = document.getElementById('session-detail');
  detail.innerHTML = emptyState('pointer', 'Select a session', 'Click a session to view its messages');
  initRunsStream();
  loadRuns();

  // Show skeleton while loading
  document.getElementById('sessions-body').innerHTML = skeleton(4, 'table');
  try {
    const res = await fetch('/api/sessions', { headers });
    const data = await res.json();
    document.getElementById('sessions-updated').textContent = 'Updated ' + relativeTime(new Date());
    const tbody = document.getElementById('sessions-body');
    if (!data.length) {
      tbody.innerHTML = `<tr><td colspan="3">${emptyState('sessions', 'No sessions yet', 'Sessions will appear here when users interact with the bot', { label: 'Start a Chat', onclick: "showPage('chat')" })}</td></tr>`;
      return;
    }
    tbody.innerHTML = data.map(s => {
      const updated = s.updated_at ? new Date(s.updated_at) : null;
      const updatedDisplay = updated ? relativeTime(updated) : '';
      const updatedFull = updated ? updated.toLocaleString() : '';
      return `<tr class="${TR} cursor-pointer" data-key="${encodeURIComponent(s.key)}"><td class="${TD}" title="${esc(s.key)}">${esc(friendlySessionKey(s.key))}</td><td class="${TD}">${s.messages || 0}</td><td class="${TD}" title="${esc(updatedFull)}">${esc(updatedDisplay)}</td></tr>`;
    }).join('');
    document.querySelectorAll('#sessions-body tr[data-key]').forEach(row => {
      row.addEventListener('click', () => viewSession(decodeURIComponent(row.dataset.key)));
    });
  } catch (e) {
    showToast('Failed to load sessions. Check your connection.', 'error');
  }
}

async function viewSession(key) {
  const detail = document.getElementById('session-detail');
  detail.className = 'whitespace-pre-wrap font-mono text-sm text-gray-500 dark:text-gray-400';
  detail.textContent = 'Loading...';
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(key)}`, { headers });
    const data = await res.json();
    if (data.error) {
      detail.textContent = data.error;
      return;
    }
    const parts = [];
    if (data.summary) {
      parts.push(`Summary:\n${data.summary}\n`);
    }
    for (const m of data.messages || []) {
      const ts = m.timestamp ? ` [${m.timestamp}]` : '';
      parts.push(`${m.role || 'unknown'}${ts}:\n${m.content}\n`);
    }
    detail.textContent = parts.join('\n');
  } catch (e) {
    showToast('Failed to load session. Check your connection.', 'error');
  }
}

// === Runs (Session Panel) ===
let runsWs = null;
let runsRefreshTimer = null;

function initRunsStream() {
  if (runsWs && runsWs.readyState <= 1) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  runsWs = new WebSocket(`${proto}//${location.host}/ws/runs?token=${TOKEN}`);
  runsWs.onmessage = e => {
    try {
      const ev = JSON.parse(e.data);
      appendRunEvent(ev);
      if (/^run_|^tool_|^compaction_/.test(ev.type || '')) {
        scheduleRunsRefresh();
      }
    } catch (_) {}
  };
  runsWs.onclose = () => { runsWs = null; };
}

function scheduleRunsRefresh() {
  if (runsRefreshTimer) clearTimeout(runsRefreshTimer);
  runsRefreshTimer = setTimeout(() => loadRuns(), 120);
}

function runStatusBadge(status) {
  if (status === 'completed') return badgeOk('completed');
  if (status === 'running') return '<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-blue-100 dark:bg-blue-500/15 text-blue-700 dark:text-blue-400">running</span>';
  if (status === 'queued') return '<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-yellow-100 dark:bg-yellow-500/15 text-yellow-700 dark:text-yellow-400">queued</span>';
  if (status === 'cancelled') return badgeDim('cancelled');
  return badgeErr(status || 'error');
}

async function loadRuns() {
  const tbody = document.getElementById('runs-body');
  const log = document.getElementById('run-events');
  if (log && !log.textContent.trim()) {
    log.textContent = '[waiting for run events]\\n';
  }
  try {
    const res = await fetch('/api/runs?limit=100', { headers });
    const data = await res.json();
    const rows = (data || []).slice(0, 30);
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="3">${emptyState('pointer', 'No runs yet', 'Runs appear here while processing')}</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map(r => {
      const runId = r.run_id || '';
      const status = r.status || '';
      const canCancel = status === 'queued' || status === 'running';
      const action = canCancel
        ? `<button class="${BTN_DANGER}" onclick="cancelRun('${esc(runId)}', this)">Cancel</button>`
        : `<span class="text-xs text-gray-400 dark:text-gray-500">-</span>`;
      return `<tr class="${TR}">
        <td class="${TD}" title="${esc(runId)}"><code>${esc(runId.slice(0, 10))}</code></td>
        <td class="${TD}">${runStatusBadge(status)}</td>
        <td class="${TD}">${action}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="3" class="${TD} text-red-500">Failed to load runs</td></tr>`;
  }
}

async function cancelRun(runId, btn) {
  const confirmed = await showConfirm('Cancel this run?');
  if (!confirmed) return;
  try {
    await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST', headers });
    if (btn) {
      btn.disabled = true;
      btn.classList.add('opacity-60');
    }
    showToast('Cancel signal sent', 'info');
    loadRuns();
  } catch (e) {
    showToast('Action failed. Please try again.', 'error');
  }
}

function appendRunEvent(ev) {
  const box = document.getElementById('run-events');
  if (!box) return;
  const tsMs = ev.ts ? Number(ev.ts) * 1000 : Date.now();
  const ts = smartTimestamp(new Date(tsMs));
  const type = ev.type || 'event';
  let detail = '';
  if (type === 'assistant_delta') {
    detail = (ev.delta || '').replace(/\s+/g, ' ').trim();
  } else if (ev.kind === 'tool') {
    detail = `${ev.tool_name || ''} ${ev.ok === false ? 'failed' : ''}`.trim();
  } else if (ev.kind === 'compaction') {
    detail = `reason=${ev.reason || ''} ${ev.ok === false ? 'failed' : ''}`.trim();
  } else if (ev.error) {
    detail = ev.error;
  } else if (ev.status) {
    detail = ev.status;
  }
  if (detail.length > 140) detail = detail.slice(0, 140) + '...';
  const line = `[${ts}] ${type}${detail ? ` :: ${detail}` : ''}`;
  const lines = box.textContent ? box.textContent.split('\n').filter(Boolean) : [];
  lines.push(line);
  while (lines.length > 200) lines.shift();
  box.textContent = lines.join('\n') + '\n';
  box.scrollTop = box.scrollHeight;
}

// === Audit ===
let auditData = [];
let auditFiltered = [];
let auditPage = 0;
const AUDIT_PER_PAGE = 25;
let auditSortCol = 'ts';
let auditSortAsc = false;

async function loadAudit() {
  document.getElementById('audit-body').innerHTML = skeleton(5, 'table');
  try {
    const res = await fetch('/api/audit?limit=500', { headers });
    auditData = (await res.json()).reverse();

    // Update timestamp
    document.getElementById('audit-updated').textContent = 'Updated ' + relativeTime(new Date());

    // Populate filter dropdown
    const filter = document.getElementById('audit-filter');
    const currentVal = filter.value;
    const types = [...new Set(auditData.map(e => e.type).filter(Boolean))].sort();
    filter.innerHTML = '<option value="">All types</option>' + types.map(t =>
      `<option value="${esc(t)}" ${t === currentVal ? 'selected' : ''}>${esc(t)}</option>`
    ).join('');

    // Update sort indicators
    updateSortIndicators();

    filterAudit();
  } catch (e) {
    showToast('Failed to load audit log. Check your connection.', 'error');
  }
}

function updateSortIndicators() {
  document.getElementById('audit-sort-ts').textContent = auditSortCol === 'ts' ? (auditSortAsc ? '▲' : '▼') : '';
  document.getElementById('audit-sort-type').textContent = auditSortCol === 'type' ? (auditSortAsc ? '▲' : '▼') : '';
}

function sortAudit(col) {
  if (auditSortCol === col) {
    auditSortAsc = !auditSortAsc;
  } else {
    auditSortCol = col;
    auditSortAsc = true;
  }
  updateSortIndicators();
  applyAuditSort();
  auditPage = 0;
  renderAuditPage();
}

function applyAuditSort() {
  auditFiltered.sort((a, b) => {
    let va, vb;
    if (auditSortCol === 'ts') {
      va = a.ts || 0;
      vb = b.ts || 0;
    } else {
      va = (a.type || '').toLowerCase();
      vb = (b.type || '').toLowerCase();
    }
    if (va < vb) return auditSortAsc ? -1 : 1;
    if (va > vb) return auditSortAsc ? 1 : -1;
    return 0;
  });
}

function filterAudit() {
  const filterVal = document.getElementById('audit-filter').value;
  auditFiltered = filterVal ? auditData.filter(e => e.type === filterVal) : [...auditData];
  applyAuditSort();
  auditPage = 0;
  renderAuditPage();
}

function renderAuditPage() {
  const tbody = document.getElementById('audit-body');
  const pagination = document.getElementById('audit-pagination');

  if (!auditFiltered.length) {
    tbody.innerHTML = `<tr><td colspan="4">${emptyState('audit', 'No audit entries', 'Activity will be logged here automatically')}</td></tr>`;
    pagination.classList.add('hidden');
    return;
  }

  const totalPages = Math.ceil(auditFiltered.length / AUDIT_PER_PAGE);
  const start = auditPage * AUDIT_PER_PAGE;
  const pageData = auditFiltered.slice(start, start + AUDIT_PER_PAGE);

  tbody.innerHTML = pageData.map(e => {
    const time = e.ts ? smartTimestamp(new Date(e.ts * 1000)) : '';
    const type = e.type || '';
    const detail = e.tool || e.event || e.dir || '';
    const status = e.ok === false ? badgeErr('failed')
      : e.ok === true ? badgeOk('success')
      : badgeDim('n/a');
    return `<tr class="${TR}"><td class="${TD}">${time}</td><td class="${TD}">${esc(type)}</td><td class="${TD} max-w-[300px] truncate">${esc(detail)}</td><td class="${TD}">${status}</td></tr>`;
  }).join('');

  if (totalPages > 1) {
    pagination.classList.remove('hidden');
    pagination.innerHTML = `
      <button class="${BTN} ${auditPage === 0 ? 'opacity-40 cursor-default' : ''}" onclick="auditPrev()" ${auditPage === 0 ? 'disabled' : ''}>&#8592; Prev</button>
      <span class="text-gray-500 dark:text-gray-400">Page ${auditPage + 1} of ${totalPages}</span>
      <button class="${BTN} ${auditPage >= totalPages - 1 ? 'opacity-40 cursor-default' : ''}" onclick="auditNext()" ${auditPage >= totalPages - 1 ? 'disabled' : ''}>Next &#8594;</button>
    `;
  } else {
    pagination.classList.add('hidden');
  }
}

function auditPrev() {
  if (auditPage > 0) { auditPage--; renderAuditPage(); }
}

function auditNext() {
  const totalPages = Math.ceil(auditFiltered.length / AUDIT_PER_PAGE);
  if (auditPage < totalPages - 1) { auditPage++; renderAuditPage(); }
}

// === Skills ===
async function loadSkills() {
  // Show detail empty state
  const detail = document.getElementById('skill-detail');
  detail.className = 'flex items-center justify-center min-h-[200px]';
  detail.innerHTML = emptyState('pointer', 'Select a skill', 'Click a skill to view its details');

  // Show skeleton while loading
  document.getElementById('skills-body').innerHTML = skeleton(3, 'table');
  try {
    const res = await fetch('/api/skills', { headers });
    const data = await res.json();
    const tbody = document.getElementById('skills-body');
    if (!data.length) {
      tbody.innerHTML = `<tr><td colspan="4">${emptyState('skills', 'No skills installed', 'Install skills using the form above', { label: 'Install a Skill', onclick: "document.getElementById('skill-source').focus()" })}</td></tr>`;
      return;
    }
    tbody.innerHTML = data.map(s => {
      const avail = s.source === 'workspace' ? 'custom' : 'built-in';
      const availableBadge = s.available === false ? badgeErr('unavailable') : badgeOk('available');
      const sec = s.secret_requirements || { required: [], present: [], missing: [] };
      const secretBadge = (sec.required || []).length
        ? ((sec.missing || []).length ? badgeErr(`secrets ${sec.present.length}/${sec.required.length}`) : badgeOk('secrets ok'))
        : badgeDim('no secrets');
      const actions = s.source === 'workspace'
        ? `<button class="${BTN_DANGER}" onclick="removeSkill('${esc(s.name)}')">Remove</button>`
        : '';
      const viewBtn = `<button class="${BTN}" onclick="viewSkill('${esc(s.name)}')">View</button>`;
      return `<tr class="${TR}">
        <td class="${TD}">${esc(s.name)}</td>
        <td class="${TD}"><div class="flex gap-1.5 flex-wrap">${badgeDim(avail)} ${availableBadge} ${secretBadge}</div></td>
        <td class="${TD} max-w-[200px] truncate">${esc(s.path)}</td>
        <td class="${TD}"><div class="flex gap-1.5">${viewBtn} ${actions}</div></td>
      </tr>`;
    }).join('');
  } catch (e) {
    showToast('Failed to load skills. Check your connection.', 'error');
  }
}

async function installSkill() {
  const source = document.getElementById('skill-source').value.trim();
  const name = document.getElementById('skill-name').value.trim();
  if (!source) { showToast('Source is required', 'error'); return; }
  const btn = document.getElementById('btn-install-skill');
  await withLoading(btn, async () => {
    const body = { source };
    if (name) body.name = name;
    const res = await fetch('/api/skills/install', { method: 'POST', headers, body: JSON.stringify(body) });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    showToast('Skill installed', 'success');
    loadSkills();
  });
}

async function removeSkill(name) {
  const confirmed = await showConfirm(`Remove skill "${name}"?`);
  if (!confirmed) return;
  try {
    await fetch(`/api/skills/${name}`, { method: 'DELETE', headers });
    showToast('Skill removed', 'success');
    loadSkills();
  } catch (e) {
    showToast('Action failed. Please try again.', 'error');
  }
}

async function viewSkill(name) {
  const detail = document.getElementById('skill-detail');
  detail.className = 'text-sm text-gray-700 dark:text-gray-300';
  detail.innerHTML = '<div class="text-gray-400 dark:text-gray-500">Loading...</div>';
  const [skillRes, secretRes] = await Promise.all([
    fetch(`/api/skills/${encodeURIComponent(name)}`, { headers }),
    fetch(`/api/skills/${encodeURIComponent(name)}/secrets`, { headers }).catch(() => null),
  ]);
  const data = await skillRes.json();
  if (data.error) {
    detail.textContent = data.error;
    return;
  }
  const secData = secretRes ? await secretRes.json() : { required: [], values: {}, present: [], missing: [] };
  const meta = data.metadata || {};
  const req = data.requires || {};
  const requiredSecrets = secData.required || [];
  const envList = (req.env || []);
  const binsList = (req.bins || []);
  const secretRows = requiredSecrets.map(envName => {
    const present = !!(secData.values || {})[envName];
    const status = present ? badgeOk('configured') : badgeErr('missing');
    return `<div class="grid grid-cols-[1fr_auto] gap-2 items-center">
      <div>
        <label class="text-xs font-medium text-gray-600 dark:text-gray-400">${esc(envName)}</label>
        <input type="password" class="form-input w-full mt-1" data-skill-secret="${esc(envName)}" placeholder="${present ? 'configured (hidden)' : 'set secret value'}">
      </div>
      <div class="pt-5">${status}</div>
    </div>`;
  }).join('');
  const saveBtn = requiredSecrets.length
    ? `<button id="btn-save-skill-secrets" class="${BTN_SUCCESS}" onclick="saveSkillSecrets('${encodeURIComponent(name)}')">Save Secrets</button>`
    : '';

  detail.innerHTML = `
    <div class="space-y-4">
      <div>
        <div class="text-sm font-semibold text-gray-800 dark:text-gray-200">Skill: ${esc(data.name || name)}</div>
      </div>
      <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-3 bg-gray-50 dark:bg-[#161922]">
        <div class="text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-2">Requirements</div>
        <div class="text-xs text-gray-600 dark:text-gray-400">CLI bins: ${binsList.length ? binsList.map(esc).join(', ') : 'none'}</div>
        <div class="text-xs text-gray-600 dark:text-gray-400 mt-1">Env secrets: ${envList.length ? envList.map(esc).join(', ') : 'none'}</div>
      </div>
      ${requiredSecrets.length ? `
      <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-3">
        <div class="text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400 mb-2">Skill Secrets</div>
        <div id="skill-secret-form" class="space-y-3">${secretRows}</div>
        <div class="flex items-center gap-2 mt-3">${saveBtn}</div>
      </div>` : ''}
      <div>
        <details>
          <summary class="cursor-pointer text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400">Metadata</summary>
          <pre class="mt-2 whitespace-pre-wrap font-mono text-xs text-gray-600 dark:text-gray-400">${esc(JSON.stringify(meta, null, 2))}</pre>
        </details>
      </div>
      <div>
        <details>
          <summary class="cursor-pointer text-xs uppercase tracking-wider text-gray-500 dark:text-gray-400">SKILL.md</summary>
          <pre class="mt-2 whitespace-pre-wrap font-mono text-xs text-gray-600 dark:text-gray-400">${esc(data.content || '')}</pre>
        </details>
      </div>
    </div>
  `;
}

async function saveSkillSecrets(encodedName) {
  const name = decodeURIComponent(encodedName);
  const form = document.getElementById('skill-secret-form');
  if (!form) return;
  const values = {};
  form.querySelectorAll('input[data-skill-secret]').forEach(input => {
    const k = input.dataset.skillSecret;
    const v = (input.value || '').trim();
    if (k && v) values[k] = v;
  });
  if (Object.keys(values).length === 0) {
    showToast('Enter at least one secret value to save', 'info');
    return;
  }
  const btn = document.getElementById('btn-save-skill-secrets');
  await withLoading(btn, async () => {
    const res = await fetch(`/api/skills/${encodeURIComponent(name)}/secrets`, {
      method: 'PUT',
      headers,
      body: JSON.stringify({ secrets: values }),
    });
    const data = await res.json();
    if (data.error) {
      showToast(data.error, 'error');
      return;
    }
    showToast('Skill secrets saved', 'success');
    await loadSkills();
    await viewSkill(name);
  });
}

// === Workspace ===
let currentWorkspaceFile = null;

async function loadWorkspace() {
  const editorEmpty = document.getElementById('workspace-editor-empty');
  const editor = document.getElementById('workspace-editor');
  const title = document.getElementById('workspace-editor-title');
  const btnSave = document.getElementById('btn-save-workspace');

  // Reset editor panel
  editor.classList.add('hidden');
  btnSave.classList.add('hidden');
  title.textContent = '';
  editorEmpty.innerHTML = emptyState('pointer', 'Select a file', 'Click a workspace file to view and edit');
  editorEmpty.classList.remove('hidden');
  currentWorkspaceFile = null;
  workspaceDirty = false;

  try {
    const res = await fetch('/api/workspace', { headers });
    const files = await res.json();
    const list = document.getElementById('workspace-file-list');
    if (!files.length) {
      list.innerHTML = '<div class="max-w-[10rem] mx-auto">' + emptyState('sessions', 'No workspace files', 'Run miniclaw onboard to create workspace files') + '</div>';
      return;
    }
    list.innerHTML = files.map(f => {
      const badge = f.exists ? badgeOk('exists') : badgeDim('missing');
      return `<div class="flex items-center gap-2 px-2 py-1.5 rounded-lg cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors workspace-file-row" data-file="${esc(f.name)}">
        <span class="text-sm flex-1 truncate">${esc(f.name)}</span>
        ${badge}
      </div>`;
    }).join('');
    list.querySelectorAll('.workspace-file-row').forEach(row => {
      row.addEventListener('click', () => selectWorkspaceFile(row.dataset.file));
    });
    // Auto-select first file
    if (files.length) selectWorkspaceFile(files[0].name);
  } catch (e) {
    showToast('Failed to load workspace files', 'error');
  }
}

async function selectWorkspaceFile(filename) {
  const editorEmpty = document.getElementById('workspace-editor-empty');
  const editor = document.getElementById('workspace-editor');
  const title = document.getElementById('workspace-editor-title');
  const btnSave = document.getElementById('btn-save-workspace');

  // Highlight selected row
  document.querySelectorAll('.workspace-file-row').forEach(r => r.classList.remove('bg-gray-100', 'dark:bg-gray-700/50'));
  const row = document.querySelector(`.workspace-file-row[data-file="${CSS.escape(filename)}"]`);
  if (row) row.classList.add('bg-gray-100', 'dark:bg-gray-700/50');

  currentWorkspaceFile = filename;
  title.textContent = filename;
  editorEmpty.classList.add('hidden');
  editor.classList.remove('hidden');
  editor.value = 'Loading...';
  editor.disabled = true;
  btnSave.classList.remove('hidden');

  try {
    const res = await fetch(`/api/workspace/${encodeURIComponent(filename)}`, { headers });
    if (!res.ok) {
      editor.value = 'Failed to load file';
      return;
    }
    const data = await res.json();
    if (data.error) {
      editor.value = data.error;
      return;
    }
    editor.value = data.content || '';
    editor.disabled = false;
    workspaceDirty = false;
  } catch (e) {
    showToast('Failed to load workspace file', 'error');
  }
}

async function saveWorkspaceFile() {
  if (!currentWorkspaceFile) return;
  const btn = document.getElementById('btn-save-workspace');
  await withLoading(btn, async () => {
    try {
      const content = document.getElementById('workspace-editor').value;
      const res = await fetch(`/api/workspace/${encodeURIComponent(currentWorkspaceFile)}`, {
        method: 'PUT', headers, body: JSON.stringify({ content })
      });
      const data = await res.json();
      if (data.error) { showToast(data.error, 'error'); return; }
      workspaceDirty = false;
      updateDirtyIndicator('workspace', false);
      showToast('Workspace file saved', 'success');
      // Refresh the file list to update exists badges
      const listRes = await fetch('/api/workspace', { headers });
      const files = await listRes.json();
      const list = document.getElementById('workspace-file-list');
      list.innerHTML = files.map(f => {
        const badge = f.exists ? badgeOk('exists') : badgeDim('missing');
        const selected = f.name === currentWorkspaceFile ? 'bg-gray-100 dark:bg-gray-700/50' : '';
        return `<div class="flex items-center gap-2 px-2 py-1.5 rounded-lg cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors workspace-file-row ${selected}" data-file="${esc(f.name)}">
          <span class="text-sm flex-1 truncate">${esc(f.name)}</span>
          ${badge}
        </div>`;
      }).join('');
      list.querySelectorAll('.workspace-file-row').forEach(row => {
        row.addEventListener('click', () => selectWorkspaceFile(row.dataset.file));
      });
    } catch (e) {
      showToast('Failed to save workspace file', 'error');
    }
  });
}

// === Memory ===
let currentMemoryFile = null;

async function loadMemory() {
  const editorEmpty = document.getElementById('memory-editor-empty');
  const editor = document.getElementById('memory-editor');
  const title = document.getElementById('memory-editor-title');
  const btnSave = document.getElementById('btn-save-memory');
  const btnExport = document.getElementById('btn-export-memory');

  // Reset editor panel
  editor.classList.add('hidden');
  btnSave.classList.add('hidden');
  btnExport.classList.add('hidden');
  title.textContent = '';
  editorEmpty.innerHTML = emptyState('pointer', 'Select a file', 'Click a memory file to view and edit');
  editorEmpty.classList.remove('hidden');
  currentMemoryFile = null;
  memoryDirty = false;

  try {
    const res = await fetch('/api/memory', { headers });
    const files = await res.json();
    const list = document.getElementById('memory-file-list');
    if (!files.length) {
      list.innerHTML = '<div class="max-w-[10rem] mx-auto">' + emptyState('sessions', 'No memory files', 'Memory files will appear as the agent learns') + '</div>';
      return;
    }
    list.innerHTML = files.map(f => {
      const badge = f.type === 'long-term' ? badgeOk('long-term') : badgeDim('daily');
      return `<div class="flex items-center gap-2 px-2 py-1.5 rounded-lg cursor-pointer hover:bg-gray-100 dark:hover:bg-gray-700/50 transition-colors memory-file-row" data-file="${esc(f.name)}">
        <span class="text-sm flex-1 truncate">${esc(f.name)}</span>
        ${badge}
      </div>`;
    }).join('');
    list.querySelectorAll('.memory-file-row').forEach(row => {
      row.addEventListener('click', () => selectMemoryFile(row.dataset.file));
    });
    // Auto-select MEMORY.md
    if (files.length) selectMemoryFile('MEMORY.md');
  } catch (e) {
    showToast('Failed to load memory files', 'error');
  }
}

async function selectMemoryFile(filename) {
  const editorEmpty = document.getElementById('memory-editor-empty');
  const editor = document.getElementById('memory-editor');
  const title = document.getElementById('memory-editor-title');
  const btnSave = document.getElementById('btn-save-memory');
  const btnExport = document.getElementById('btn-export-memory');

  // Highlight selected row
  document.querySelectorAll('.memory-file-row').forEach(r => r.classList.remove('bg-gray-100', 'dark:bg-gray-700/50'));
  const row = document.querySelector(`.memory-file-row[data-file="${CSS.escape(filename)}"]`);
  if (row) row.classList.add('bg-gray-100', 'dark:bg-gray-700/50');

  currentMemoryFile = filename;
  title.textContent = filename;
  editorEmpty.classList.add('hidden');
  editor.classList.remove('hidden');
  editor.value = 'Loading...';
  editor.disabled = true;
  btnSave.classList.remove('hidden');
  if (filename === 'MEMORY.md') {
    btnExport.classList.remove('hidden');
  } else {
    btnExport.classList.add('hidden');
  }

  try {
    const res = await fetch(`/api/memory/${encodeURIComponent(filename)}`, { headers });
    const data = await res.json();
    if (data.error) {
      editor.value = data.error;
      return;
    }
    editor.value = data.content || '';
    editor.disabled = false;
    memoryDirty = false;
  } catch (e) {
    showToast('Failed to load memory file', 'error');
  }
}

async function saveMemoryFile() {
  if (!currentMemoryFile) return;
  const btn = document.getElementById('btn-save-memory');
  await withLoading(btn, async () => {
    try {
      const content = document.getElementById('memory-editor').value;
      const res = await fetch(`/api/memory/${encodeURIComponent(currentMemoryFile)}`, {
        method: 'PUT', headers, body: JSON.stringify({ content })
      });
      const data = await res.json();
      if (data.error) { showToast(data.error, 'error'); return; }
      memoryDirty = false;
      updateDirtyIndicator('memory', false);
      showToast('Memory saved', 'success');
    } catch (e) {
      showToast('Failed to save memory file', 'error');
    }
  });
}

function exportMemory() {
  const content = document.getElementById('memory-editor').value;
  const blob = new Blob([content], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = currentMemoryFile || 'MEMORY.md';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// === Heartbeat ===
function formatInterval(seconds) {
  if (!seconds) return 'Unknown';
  if (seconds < 60) return `Every ${seconds} seconds`;
  const min = Math.round(seconds / 60);
  if (min < 60) return `Every ${min} minute${min !== 1 ? 's' : ''}`;
  const hrs = Math.round(min / 60);
  return `Every ${hrs} hour${hrs !== 1 ? 's' : ''}`;
}

function heartbeatStatusRow(indicator, label, value) {
  return `<div class="flex items-center gap-2.5 py-2 border-b border-gray-100 dark:border-gray-800 last:border-0">
    <div class="shrink-0 w-5 flex justify-center">${indicator}</div>
    <span class="text-xs font-medium text-gray-500 dark:text-gray-400 w-16 shrink-0">${label}</span>
    <span class="text-sm flex-1">${value}</span>
  </div>`;
}

async function loadHeartbeat() {
  const statusEl = document.getElementById('heartbeat-status');
  const editor = document.getElementById('heartbeat-editor');
  if (!statusEl || !editor) return;
  statusEl.innerHTML = '<div class="text-sm text-gray-400">Loading...</div>';
  try {
    const res = await fetch('/api/heartbeat', { headers });
    const data = await res.json();
    if (!data.ok) {
      statusEl.innerHTML = `<div class="text-sm text-gray-400">${esc(data.error || 'Heartbeat not available')}</div>`;
      editor.value = '';
      return;
    }
    const s = data.status || {};

    // Dots
    const greenDot = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-green-500"></span>';
    const grayDot = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>';
    const bluePulseDot = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-blue-500 heartbeat-pulse"></span>';
    const blueStaticDot = '<span class="inline-block w-2.5 h-2.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>';

    // SVG icons (w-4 h-4)
    const clockIcon = '<svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>';
    const clockCheckIcon = '<svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M9 12l2 2 4-4"/></svg>';
    const arrowIcon = '<svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M5 12h14m-7-7l7 7-7 7"/></svg>';
    const playIcon = '<svg class="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>';

    // Build values
    const enabledVal = s.enabled ? 'Enabled' : 'Disabled';
    const serviceVal = s.running ? 'Running' : 'Stopped';

    const intervalVal = formatInterval(s.interval_s);

    let lastRunVal = 'Never';
    if (s.last_run_at_ms) {
      const d = new Date(s.last_run_at_ms);
      lastRunVal = `${smartTimestamp(d)} <span class="text-xs text-gray-400 dark:text-gray-500 ml-1">(${relativeTime(d)})</span>`;
    }

    let nextRunVal = 'N/A';
    if (s.next_run_at_ms) {
      const d = new Date(s.next_run_at_ms);
      nextRunVal = `${smartTimestamp(d)} <span class="text-xs text-gray-400 dark:text-gray-500 ml-1">(${relativeTimeFromNow(d)})</span>`;
    }

    let startedVal = 'N/A';
    if (s.started_at_ms) {
      const d = new Date(s.started_at_ms);
      startedVal = `${smartTimestamp(d)} <span class="text-xs text-gray-400 dark:text-gray-500 ml-1">(${relativeTime(d)})</span>`;
    }

    statusEl.innerHTML =
      heartbeatStatusRow(s.enabled ? greenDot : grayDot, 'Status', enabledVal) +
      heartbeatStatusRow(s.running ? bluePulseDot : blueStaticDot, 'Service', serviceVal) +
      heartbeatStatusRow(clockIcon, 'Interval', intervalVal) +
      heartbeatStatusRow(clockCheckIcon, 'Last Run', lastRunVal) +
      heartbeatStatusRow(arrowIcon, 'Next Run', nextRunVal) +
      heartbeatStatusRow(playIcon, 'Started', startedVal);

    editor.value = data.content || '';
    heartbeatDirty = false;
  } catch (e) {
    statusEl.innerHTML = '<div class="text-sm text-red-400">Failed to load heartbeat</div>';
    showToast('Failed to load heartbeat', 'error');
  }
}

async function saveHeartbeat() {
  const btn = document.getElementById('btn-save-heartbeat');
  if (!btn) return;
  await withLoading(btn, async () => {
    try {
      const content = document.getElementById('heartbeat-editor').value;
      const res = await fetch('/api/heartbeat', {
        method: 'PUT', headers, body: JSON.stringify({ content })
      });
      const data = await res.json();
      if (!data.ok) { showToast(data.error || 'Failed to save heartbeat', 'error'); return; }
      heartbeatDirty = false;
      updateDirtyIndicator('heartbeat', false);
      showToast('Heartbeat saved', 'success');
    } catch (e) {
      showToast('Failed to save heartbeat', 'error');
    }
  });
}

async function triggerHeartbeat() {
  const btn = document.getElementById('btn-trigger-heartbeat');
  if (!btn) return;
  await withLoading(btn, async () => {
    try {
      const res = await fetch('/api/heartbeat/trigger', { method: 'POST', headers });
      const data = await res.json();
      if (!data.ok) { showToast(data.error || 'Failed to run heartbeat', 'error'); return; }
      showToast('Heartbeat triggered', 'success');
      loadHeartbeat();
    } catch (e) {
      showToast('Failed to run heartbeat', 'error');
    }
  });
}

// === Cron ===
async function loadCron() {
  // Initialize cron builder on first load
  updateCronBuilder();
  document.getElementById('cron-body').innerHTML = skeleton(3, 'table');
  try {
    const res = await fetch('/api/cron', { headers });
    const data = await res.json();
    const tbody = document.getElementById('cron-body');
    if (!data.length) {
      tbody.innerHTML = `<tr><td colspan="5">${emptyState('cron', 'No scheduled tasks', 'Add a task using the form above', { label: 'Create a Job', onclick: "document.getElementById('cron-message').focus()" })}</td></tr>`;
    } else {
      tbody.innerHTML = data.map(j => {
        const badge = j.enabled ? badgeOk('on') : badgeDim('off');
        const nextRunDate = j.next_run_at_ms ? new Date(j.next_run_at_ms) : null;
        const nextRunRelative = nextRunDate ? relativeTimeFromNow(nextRunDate) : '';
        const nextRunFull = nextRunDate ? nextRunDate.toLocaleString() : '';
        const toggle = j.enabled
          ? `<button class="${BTN}" onclick="toggleCron('${j.id}', false)">Disable</button>`
          : `<button class="${BTN}" onclick="toggleCron('${j.id}', true)">Enable</button>`;
        return `<tr class="${TR}">
          <td class="${TD}">${esc(j.name)}</td>
          <td class="${TD}">${badge}</td>
          <td class="${TD}">${esc(j.kind)}</td>
          <td class="${TD}">${esc(j.schedule)}<div class="text-xs text-gray-400 dark:text-gray-500 mt-0.5" title="${esc(nextRunFull)}">${esc(nextRunRelative)}</div></td>
          <td class="${TD}"><div class="flex gap-1.5">${toggle} <button class="${BTN_DANGER}" onclick="removeCron('${j.id}')">Remove</button></div></td>
        </tr>`;
      }).join('');
    }
  } catch (e) {
    showToast('Failed to load scheduled tasks. Check your connection.', 'error');
  }
}

async function removeCron(id) {
  const confirmed = await showConfirm('Remove this scheduled task?');
  if (!confirmed) return;
  try {
    await fetch(`/api/cron/${id}`, { method: 'DELETE', headers });
    showToast('Task removed', 'success');
    loadCron();
  } catch (e) {
    showToast('Action failed. Please try again.', 'error');
  }
}

async function toggleCron(id, enabled) {
  try {
    await fetch(`/api/cron/${id}/enable`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ enabled })
    });
    loadCron();
  } catch (e) {
    showToast('Action failed. Please try again.', 'error');
  }
}

// --- Visual cron builder ---
function updateCronBuilder() {
  const freq = document.getElementById('cron-frequency').value;
  const container = document.getElementById('cron-builder-fields');
  if (!container) return;
  let html = '';
  if (freq === 'minutes') {
    html = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1" for="cron-b-minutes">Every</label>
      <div class="flex items-center gap-1.5"><input id="cron-b-minutes" type="number" min="1" max="59" value="5" class="form-input flex-1" oninput="syncCronBuilder()"><span class="text-xs text-gray-500 dark:text-gray-400">minutes</span></div>`;
  } else if (freq === 'hourly') {
    html = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1" for="cron-b-minute">At minute</label>
      <input id="cron-b-minute" type="number" min="0" max="59" value="0" class="form-input w-full" oninput="syncCronBuilder()">`;
  } else if (freq === 'daily') {
    html = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Time</label>
      <div class="flex items-center gap-1.5">
        <input id="cron-b-hour" type="number" min="0" max="23" value="9" class="form-input flex-1" oninput="syncCronBuilder()">
        <span class="text-xs text-gray-500 dark:text-gray-400">:</span>
        <input id="cron-b-min" type="number" min="0" max="59" value="0" class="form-input flex-1" oninput="syncCronBuilder()">
      </div>`;
  } else if (freq === 'weekly') {
    html = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Day</label>
      <select id="cron-b-day" class="form-input w-full mb-2" onchange="syncCronBuilder()">
        <option value="1">Monday</option><option value="2">Tuesday</option><option value="3">Wednesday</option>
        <option value="4">Thursday</option><option value="5">Friday</option><option value="6">Saturday</option><option value="0">Sunday</option>
      </select>
      <label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Time</label>
      <div class="flex items-center gap-1.5">
        <input id="cron-b-hour" type="number" min="0" max="23" value="9" class="form-input flex-1" oninput="syncCronBuilder()">
        <span class="text-xs text-gray-500 dark:text-gray-400">:</span>
        <input id="cron-b-min" type="number" min="0" max="59" value="0" class="form-input flex-1" oninput="syncCronBuilder()">
      </div>`;
  } else if (freq === 'interval') {
    html = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1" for="cron-b-seconds">Repeat every</label>
      <div class="flex items-center gap-1.5"><input id="cron-b-seconds" type="number" min="1" value="3600" class="form-input flex-1" oninput="syncCronBuilder()"><span class="text-xs text-gray-500 dark:text-gray-400">seconds</span></div>`;
  } else if (freq === 'cron') {
    html = `<label class="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1" for="cron-b-expr">Cron expression</label>
      <input id="cron-b-expr" type="text" placeholder="e.g. */5 * * * *" class="form-input w-full" oninput="syncCronBuilder()">
      <p class="text-xs text-gray-400 dark:text-gray-500 mt-1">Standard 5-field cron: minute hour day month weekday</p>`;
  }
  container.innerHTML = html;
  syncCronBuilder();
}

function syncCronBuilder() {
  const freq = document.getElementById('cron-frequency').value;
  const everyEl = document.getElementById('cron-every');
  const cronEl = document.getElementById('cron-cron');
  const preview = document.getElementById('cron-preview');
  everyEl.value = '';
  cronEl.value = '';
  let previewText = '';

  if (freq === 'minutes') {
    const mins = parseInt(document.getElementById('cron-b-minutes')?.value) || 5;
    cronEl.value = `*/${mins} * * * *`;
    previewText = `Runs every ${mins} minute${mins !== 1 ? 's' : ''}`;
  } else if (freq === 'hourly') {
    const min = parseInt(document.getElementById('cron-b-minute')?.value) || 0;
    cronEl.value = `${min} * * * *`;
    previewText = `Runs every hour at :${String(min).padStart(2, '0')}`;
  } else if (freq === 'daily') {
    const hour = parseInt(document.getElementById('cron-b-hour')?.value) || 0;
    const min = parseInt(document.getElementById('cron-b-min')?.value) || 0;
    cronEl.value = `${min} ${hour} * * *`;
    const ampm = hour >= 12 ? 'PM' : 'AM';
    const h12 = hour % 12 || 12;
    previewText = `Runs every day at ${h12}:${String(min).padStart(2, '0')} ${ampm}`;
  } else if (freq === 'weekly') {
    const day = document.getElementById('cron-b-day')?.value || '1';
    const hour = parseInt(document.getElementById('cron-b-hour')?.value) || 0;
    const min = parseInt(document.getElementById('cron-b-min')?.value) || 0;
    cronEl.value = `${min} ${hour} * * ${day}`;
    const dayNames = { '0': 'Sunday', '1': 'Monday', '2': 'Tuesday', '3': 'Wednesday', '4': 'Thursday', '5': 'Friday', '6': 'Saturday' };
    const ampm = hour >= 12 ? 'PM' : 'AM';
    const h12 = hour % 12 || 12;
    previewText = `Runs every ${dayNames[day] || 'week'} at ${h12}:${String(min).padStart(2, '0')} ${ampm}`;
  } else if (freq === 'interval') {
    const secs = parseInt(document.getElementById('cron-b-seconds')?.value) || 3600;
    everyEl.value = secs;
    if (secs < 60) previewText = `Runs every ${secs} second${secs !== 1 ? 's' : ''}`;
    else if (secs < 3600) { const m = Math.round(secs / 60); previewText = `Runs every ${m} minute${m !== 1 ? 's' : ''}`; }
    else { const h = Math.round(secs / 3600); previewText = `Runs every ${h} hour${h !== 1 ? 's' : ''}`; }
  } else if (freq === 'cron') {
    const expr = document.getElementById('cron-b-expr')?.value || '';
    cronEl.value = expr;
    previewText = expr ? `Cron: ${expr}` : '';
  }
  if (preview) preview.textContent = previewText;
}

async function addCron() {
  const message = document.getElementById('cron-message').value.trim();
  const every = document.getElementById('cron-every').value.trim();
  const cronExpr = document.getElementById('cron-cron').value.trim();
  const kind = document.getElementById('cron-kind').value;
  const channel = document.getElementById('cron-channel').value.trim();
  const to = document.getElementById('cron-to').value.trim();
  const isolated = document.getElementById('cron-isolated').checked;
  if (!message) { showToast('Message is required', 'error'); return; }
  const btn = document.getElementById('btn-add-cron');
  await withLoading(btn, async () => {
    const body = { message, kind, isolated };
    if (every) body.every_seconds = parseInt(every, 10);
    if (cronExpr) body.cron_expr = cronExpr;
    if (channel) body.channel = channel;
    if (to) body.to = to;
    const res = await fetch('/api/cron', { method: 'POST', headers, body: JSON.stringify(body) });
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }
    showToast('Task added', 'success');
    loadCron();
  });
}

// === Status ===
function statusRow(label, value) {
  return `<div class="flex items-center justify-between py-1.5 border-b border-gray-100 dark:border-gray-800 last:border-0">
    <span class="text-xs text-gray-500 dark:text-gray-400">${esc(label)}</span>
    <span class="text-sm font-medium">${value}</span>
  </div>`;
}

async function loadStatus() {
  try {
    const res = await fetch('/api/status', { headers });
    const data = await res.json();
    const cards = document.getElementById('status-cards');
    const topParts = [];
    if (data.model) topParts.push(statusCard('Model', esc(data.model)));
    topParts.push(statusCard('Approvals', data.approvals_enabled ? 'on' : 'off'));
    topParts.push(statusCard('Rate Limit', data.rate_limit_enabled ? 'on' : 'off'));
    if (data.cron) topParts.push(statusCard('Scheduled Tasks', data.cron.jobs || 0));
    if (data.heartbeat) topParts.push(statusCard('Heartbeat', data.heartbeat.running ? 'running' : 'stopped'));
    if (data.channels) {
      for (const [name, st] of Object.entries(data.channels)) {
        topParts.push(statusCard(`Channel: ${esc(name)}`, st.running ? 'running' : 'stopped'));
      }
    }
    if (data.runs) topParts.push(statusCard('Active Runs', data.runs.active || 0));
    cards.innerHTML = topParts.join('');

    // Structured content
    const content = document.getElementById('status-content');
    const sections = [];

    // AI Model card
    if (data.model || data.version) {
      let rows = '';
      if (data.model) rows += statusRow('Model', esc(data.model));
      if (data.version) rows += statusRow('Version', esc(data.version));
      rows += statusRow('Status', data.runs && data.runs.active > 0 ? badgeOk('Active') : badgeDim('Idle'));
      sections.push(`<div class="panel-card p-4"><div class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">AI Model</div>${rows}</div>`);
    }

    // Channels card
    if (data.channels && Object.keys(data.channels).length) {
      let rows = '';
      for (const [name, st] of Object.entries(data.channels)) {
        const dot = st.running
          ? '<span class="inline-block w-2 h-2 rounded-full bg-green-500 mr-1.5"></span>'
          : '<span class="inline-block w-2 h-2 rounded-full bg-red-400 mr-1.5"></span>';
        rows += statusRow(name.charAt(0).toUpperCase() + name.slice(1), dot + (st.running ? 'Connected' : 'Disconnected'));
      }
      sections.push(`<div class="panel-card p-4"><div class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Communication Channels</div>${rows}</div>`);
    }

    // Security card
    let secRows = '';
    secRows += statusRow('Approvals', data.approvals_enabled ? badgeOk('Enabled') : badgeDim('Disabled'));
    secRows += statusRow('Rate Limit', data.rate_limit_enabled ? badgeOk('Enabled') : badgeDim('Disabled'));
    if (data.tools) {
      const profile = data.tools.approval_profile || data.tools.approvalProfile || '';
      if (profile) {
        const fl = FRIENDLY_LABELS[profile];
        secRows += statusRow('Approval Profile', esc(fl ? fl.label : profile));
      }
    }
    sections.push(`<div class="panel-card p-4"><div class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Security</div>${secRows}</div>`);

    // Activity card
    if (data.runs || data.cron) {
      let actRows = '';
      if (data.runs) {
        actRows += statusRow('Active Runs', esc(String(data.runs.active || 0)));
        if (data.runs.total !== undefined) actRows += statusRow('Total Runs', esc(String(data.runs.total)));
      }
      if (data.cron) actRows += statusRow('Scheduled Tasks', esc(String(data.cron.jobs || 0)));
      if (data.heartbeat) actRows += statusRow('Heartbeat', data.heartbeat.running ? badgeOk('Running') : badgeDim('Stopped'));
      sections.push(`<div class="panel-card p-4"><div class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-2">Activity</div>${actRows}</div>`);
    }

    // Raw JSON
    sections.push(`<details class="mt-2"><summary class="cursor-pointer text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider hover:text-gray-700 dark:hover:text-gray-300 transition-colors">Raw JSON Data</summary><pre class="mt-2 font-mono text-xs text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-[#161922] border border-gray-200 dark:border-gray-700 rounded-lg p-3 overflow-auto max-h-[400px] whitespace-pre-wrap">${esc(JSON.stringify(data, null, 2))}</pre></details>`);

    content.innerHTML = `<div class="grid grid-cols-1 md:grid-cols-2 gap-4">${sections.slice(0, -1).join('')}</div>${sections[sections.length - 1]}`;
    document.getElementById('status-updated').textContent = 'Updated ' + relativeTime(new Date());
  } catch (e) {
    showToast('Failed to load status. Check your connection.', 'error');
  }
}

// === Approvals ===
let approvalsWs = null;

function initApprovals() {
  if (approvalsWs && approvalsWs.readyState <= 1) return;
  loadApprovals();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  approvalsWs = new WebSocket(`${proto}//${location.host}/ws/approvals?token=${TOKEN}`);
  approvalsWs.onmessage = e => {
    const ev = JSON.parse(e.data);
    addApprovalItem(ev);
  };
  approvalsWs.onclose = () => { approvalsWs = null; };
}

async function loadApprovals() {
  document.getElementById('approvals-list').innerHTML = skeleton(3, 'lines');
  try {
    const res = await fetch('/api/approvals/pending', { headers });
    const data = await res.json();
    const list = document.getElementById('approvals-list');
    list.innerHTML = '';
    if (!(data || []).length) {
      list.innerHTML = emptyState('approvals', 'No pending approvals', 'Approval requests from the agent will appear here');
      return;
    }
    (data || []).forEach(addApprovalItem);
  } catch (e) {
    showToast('Failed to load approvals. Check your connection.', 'error');
  }
}

function addApprovalItem(ev) {
  const list = document.getElementById('approvals-list');
  // Remove empty state if present
  const existingEmpty = list.querySelector('.flex.flex-col.items-center');
  if (existingEmpty) existingEmpty.remove();

  const item = document.createElement('div');
  item.className = 'panel-card p-4';
  const params = JSON.stringify(ev.params || {}, null, 2);
  const id = encodeURIComponent(ev.id || '');
  const sk = encodeURIComponent(ev.session_key || '');
  item.innerHTML = `
    <div class="mb-2"><span class="font-medium">${esc(ev.tool)}</span> <span class="text-xs text-gray-400 dark:text-gray-500">(${esc(ev.channel)}:${esc(ev.chat_id)})</span></div>
    <pre class="bg-gray-900 dark:bg-[#0b0d13] border border-gray-200 dark:border-gray-700 rounded-lg p-3 font-mono text-xs whitespace-pre-wrap text-gray-300 my-2">${esc(params)}</pre>
    <div class="flex gap-2">
      <button class="${BTN_SUCCESS}" onclick="respondApproval('${id}', '${sk}', 'approve', this)">Approve</button>
      <button class="${BTN_DANGER}" onclick="respondApproval('${id}', '${sk}', 'deny', this)">Deny</button>
    </div>
  `;
  list.prepend(item);
}

async function respondApproval(id, sessionKey, decision, btn) {
  if (decision === 'deny') {
    const confirmed = await showConfirm('Deny this approval?');
    if (!confirmed) return;
  }
  try {
    await fetch('/api/approvals/respond', {
      method: 'POST',
      headers,
      body: JSON.stringify({ id: decodeURIComponent(id), session_key: decodeURIComponent(sessionKey), decision })
    });
    const item = btn.closest('.panel-card');
    if (item) item.remove();
    showToast(decision === 'approve' ? 'Approved' : 'Denied', decision === 'approve' ? 'success' : 'info');

    // Show empty state if no more items
    const list = document.getElementById('approvals-list');
    if (!list.children.length) {
      list.innerHTML = emptyState('approvals', 'No pending approvals', 'Approval requests from the agent will appear here');
    }
  } catch (e) {
    showToast('Action failed. Please try again.', 'error');
  }
}

// Helpers
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

// === Theme ===
function applyTheme(dark) {
  if (dark) {
    document.documentElement.dataset.theme = 'dark';
  } else {
    delete document.documentElement.dataset.theme;
  }
}

function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved === 'dark') {
    applyTheme(true);
  } else if (saved === 'light') {
    applyTheme(false);
  } else {
    // Follow system preference
    applyTheme(window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  // Live-update when system preference changes (only if no explicit override)
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
    if (!localStorage.getItem('theme')) {
      applyTheme(e.matches);
    }
  });
}

function toggleTheme() {
  const isDark = document.documentElement.dataset.theme === 'dark';
  if (isDark) {
    applyTheme(false);
    localStorage.setItem('theme', 'light');
  } else {
    applyTheme(true);
    localStorage.setItem('theme', 'dark');
  }
}

// === Quick-nav palette ===
function openQuickNav() {
  const overlay = document.getElementById('quick-nav-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  const input = document.getElementById('quick-nav-input');
  if (input) { input.value = ''; input.focus(); }
  renderQuickNavList('');
}

function closeQuickNav() {
  const overlay = document.getElementById('quick-nav-overlay');
  if (overlay) overlay.classList.add('hidden');
}

function renderQuickNavList(filter) {
  const list = document.getElementById('quick-nav-list');
  if (!list) return;
  const lowerFilter = filter.toLowerCase();
  const entries = Object.entries(PAGE_NAMES).filter(([, name]) =>
    !lowerFilter || name.toLowerCase().includes(lowerFilter)
  );
  if (!entries.length) {
    list.innerHTML = '<div class="px-3 py-4 text-sm text-gray-400 dark:text-gray-500 text-center">No matches</div>';
    return;
  }
  list.innerHTML = entries.map(([id, name], i) =>
    `<div class="quick-nav-item${i === 0 ? ' active' : ''}" data-page="${id}">${esc(name)}</div>`
  ).join('');
  list.querySelectorAll('.quick-nav-item').forEach(item => {
    item.addEventListener('click', () => {
      closeQuickNav();
      showPage(item.dataset.page);
    });
  });
}

// Quick-nav keyboard navigation
document.addEventListener('keydown', e => {
  const overlay = document.getElementById('quick-nav-overlay');
  if (!overlay || overlay.classList.contains('hidden')) return;
  const items = overlay.querySelectorAll('.quick-nav-item');
  if (!items.length) return;
  const active = overlay.querySelector('.quick-nav-item.active');
  let idx = Array.from(items).indexOf(active);

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    items.forEach(i => i.classList.remove('active'));
    idx = (idx + 1) % items.length;
    items[idx].classList.add('active');
    items[idx].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    items.forEach(i => i.classList.remove('active'));
    idx = (idx - 1 + items.length) % items.length;
    items[idx].classList.add('active');
    items[idx].scrollIntoView({ block: 'nearest' });
  } else if (e.key === 'Enter' && active) {
    e.preventDefault();
    closeQuickNav();
    showPage(active.dataset.page);
  }
});

// === Global keyboard shortcuts ===
document.addEventListener('keydown', e => {
  const isMod = e.metaKey || e.ctrlKey;

  // Ctrl/Cmd+K → toggle quick-nav
  if (isMod && e.key === 'k') {
    e.preventDefault();
    const overlay = document.getElementById('quick-nav-overlay');
    if (overlay && !overlay.classList.contains('hidden')) {
      closeQuickNav();
    } else {
      openQuickNav();
    }
    return;
  }

  // Escape → close quick-nav
  if (e.key === 'Escape') {
    const overlay = document.getElementById('quick-nav-overlay');
    if (overlay && !overlay.classList.contains('hidden')) {
      e.preventDefault();
      closeQuickNav();
      return;
    }
  }

  // Ctrl/Cmd+S → save on applicable pages
  if (isMod && e.key === 's') {
    e.preventDefault();
    if (currentPageId === 'config') saveConfig();
    else if (currentPageId === 'memory') saveMemoryFile();
    else if (currentPageId === 'heartbeat') saveHeartbeat();
    else if (currentPageId === 'workspace') saveWorkspaceFile();
  }
});

// === Responsive sidebar ===
function toggleSidebarMobile(show) {
  const sidebar = document.getElementById('main-sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (!sidebar) return;
  if (show) {
    sidebar.classList.add('mobile-open');
    if (backdrop) backdrop.classList.add('visible');
  } else {
    sidebar.classList.remove('mobile-open');
    if (backdrop) backdrop.classList.remove('visible');
  }
}

// === Guided Tour ===
const TOUR_STEPS = [
  { target: '[data-page="dashboard"]', title: 'Dashboard', desc: 'Your home page. See a quick overview of everything -- active model, channels, recent activity, and quick navigation.', page: 'dashboard' },
  { target: '[data-page="chat"]', title: 'Chat', desc: 'Talk to your AI agent in real time. Messages go through the same pipeline as WhatsApp or Telegram.', page: 'chat' },
  { target: '[data-page="config"]', title: 'Configuration', desc: 'All the settings for your bot -- model, channels, tools, security, and more. Look for the (i) icons for extra help.', page: 'config' },
  { target: '[data-page="sessions"]', title: 'Sessions', desc: 'Review conversation history and monitor active processing runs.', page: 'sessions' },
  { target: '[data-page="audit"]', title: 'Audit Log', desc: 'A complete record of everything the AI has done -- every tool call, message, and command.', page: 'audit' },
  { target: '[data-page="skills"]', title: 'Skills', desc: 'Plug-in capabilities for your AI. Install skills to teach it new abilities.', page: 'skills' },
  { target: '[data-page="workspace"]', title: 'Workspace', desc: 'Edit the files that define your AI\'s personality (SOUL.md) and behavior.', page: 'workspace' },
  { target: '[data-page="memory"]', title: 'Memory', desc: 'The AI\'s long-term memory. Review and edit what it remembers from conversations.', page: 'memory' },
  { target: '[data-page="cron"]', title: 'Scheduled Tasks', desc: 'Set up recurring jobs -- daily reports, reminders, periodic check-ins.', page: 'cron' },
  { target: '[data-page="heartbeat"]', title: 'Heartbeat', desc: 'A periodic self-check where the AI reviews its instructions and pending tasks.', page: 'heartbeat' },
  { target: '[data-page="approvals"]', title: 'Approvals', desc: 'When the AI wants to do something risky, it asks here first. Approve or deny each action.', page: 'approvals' },
  { target: '[data-page="status"]', title: 'Status', desc: 'Live system health -- running model, connected channels, and diagnostics.', page: 'status' },
  { target: '#theme-toggle', title: 'Theme Toggle', desc: 'Switch between light and dark mode. Your preference is saved automatically.' },
];

let tourStep = -1;
let tourActive = false;

function startTour() {
  // Expand sidebar if collapsed
  if (document.body.classList.contains('sidebar-collapsed') && window.innerWidth >= 640) {
    document.body.classList.remove('sidebar-collapsed');
    localStorage.setItem('sidebar-collapsed', 'false');
  }
  tourStep = 0;
  tourActive = true;
  showTourStep();
}

function showTourStep() {
  // Remove previous tour elements
  document.querySelectorAll('.tour-overlay-bg, .tour-spotlight, .tour-panel').forEach(el => el.remove());
  if (tourStep < 0 || tourStep >= TOUR_STEPS.length) { endTour(); return; }
  const step = TOUR_STEPS[tourStep];
  const target = document.querySelector(step.target);
  if (!target) { nextTourStep(); return; }

  // Scroll target into view
  target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  const rect = target.getBoundingClientRect();
  const pad = 6;

  // Overlay background (click to dismiss)
  const bg = document.createElement('div');
  bg.className = 'tour-overlay-bg';
  bg.onclick = () => endTour();
  document.body.appendChild(bg);

  // Spotlight cutout
  const spot = document.createElement('div');
  spot.className = 'tour-spotlight';
  spot.style.cssText = `top:${rect.top - pad}px;left:${rect.left - pad}px;width:${rect.width + pad * 2}px;height:${rect.height + pad * 2}px;`;
  document.body.appendChild(spot);

  // Panel
  const panel = document.createElement('div');
  panel.className = 'tour-panel';
  const progress = `${tourStep + 1} / ${TOUR_STEPS.length}`;
  const prevBtn = tourStep > 0 ? `<button class="${BTN}" onclick="prevTourStep()">Back</button>` : '';
  const nextLabel = tourStep < TOUR_STEPS.length - 1 ? 'Next' : 'Finish';
  panel.innerHTML = `
    <div class="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-1">${esc(step.title)}</div>
    <div class="text-xs text-gray-500 dark:text-gray-400 leading-relaxed mb-3">${esc(step.desc)}</div>
    <div class="flex items-center justify-between">
      <span class="text-xs text-gray-400 dark:text-gray-500">${progress}</span>
      <div class="flex gap-1.5">
        <button class="${BTN}" onclick="endTour()">Skip</button>
        ${prevBtn}
        <button class="${BTN_SUCCESS}" onclick="nextTourStep()">${nextLabel}</button>
      </div>
    </div>`;

  // Position panel below or above the target
  const panelTop = rect.bottom + pad + 10;
  const panelLeft = Math.max(8, Math.min(rect.left, window.innerWidth - 340));
  if (panelTop + 160 > window.innerHeight) {
    // Show above
    panel.style.cssText = `bottom:${window.innerHeight - rect.top + pad + 10}px;left:${panelLeft}px;`;
  } else {
    panel.style.cssText = `top:${panelTop}px;left:${panelLeft}px;`;
  }
  document.body.appendChild(panel);
}

function nextTourStep() {
  tourStep++;
  if (tourStep >= TOUR_STEPS.length) { endTour(); return; }
  showTourStep();
}

function prevTourStep() {
  if (tourStep > 0) { tourStep--; showTourStep(); }
}

function endTour() {
  tourActive = false;
  tourStep = -1;
  document.querySelectorAll('.tour-overlay-bg, .tour-spotlight, .tour-panel').forEach(el => el.remove());
  localStorage.setItem('tour_completed', 'true');
}

// Init
initTheme();
initSidebar();
// Auto-collapse on mobile
if (window.innerWidth < 640) {
  document.body.classList.add('sidebar-collapsed');
}
window.addEventListener('beforeunload', e => {
  if (hasDirtyState()) { e.preventDefault(); e.returnValue = ''; }
});
document.getElementById('config-editor').addEventListener('input', () => { configDirty = true; updateDirtyIndicator('config', true); });
document.getElementById('memory-editor').addEventListener('input', () => { memoryDirty = true; updateDirtyIndicator('memory', true); });
document.getElementById('heartbeat-editor').addEventListener('input', () => { heartbeatDirty = true; updateDirtyIndicator('heartbeat', true); });
document.getElementById('workspace-editor').addEventListener('input', () => { workspaceDirty = true; updateDirtyIndicator('workspace', true); });
doShowPage('dashboard');
// Auto-start tour on first visit
if (!localStorage.getItem('tour_completed')) {
  setTimeout(() => startTour(), 600);
}
