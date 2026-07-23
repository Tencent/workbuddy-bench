// Shared model roster + track definitions — SINGLE SOURCE for the homepage
// leaderboard (app_v2.js) and every per-subset leaderboard (subset-lb.js).
// Scores live in data.js; this file only defines who's on the board and the tracks.
//   real=true  -> has scores in data.js
//   real=false -> roster member, evaluation pending
//   access     -> which harnesses the model can run on
//   track.preview=true -> shown as a column but excluded from the cross-track Average
window.WB_ROSTER = [
  // --- proprietary models — both harnesses usable ---
  { key: "Claude Opus 4.8",   label: "Claude Opus 4.8",   vendor: "Anthropic", type: "proprietary", access: "CodeBuddy Code · Claude Code", real: true  },
  { key: "GPT-5.5",           label: "GPT-5.5",           vendor: "OpenAI",    type: "proprietary", access: "CodeBuddy Code · Claude Code", real: true  },
  // --- open-weight models ---
  { key: "GLM-5.2",           label: "GLM-5.2",           vendor: "Zhipu",     type: "open",        access: "CodeBuddy Code · Claude Code", real: true  },
  { key: "DeepSeek-V4-Pro",   label: "DeepSeek-V4-Pro",   vendor: "DeepSeek",  type: "open",        access: "CodeBuddy Code · Claude Code", real: true  },
  { key: "DeepSeek-v4-flash", label: "DeepSeek-V4-Flash", vendor: "DeepSeek",  type: "open",        access: "CodeBuddy Code · Claude Code", real: true  },
  { key: "MiniMax-M3",        label: "MiniMax-M3",        vendor: "MiniMax",   type: "open",        access: "CodeBuddy Code · Claude Code", real: true  },
  { key: "HY-3",               label: "HY-3",         vendor: "Tencent",   brand: "Hunyuan", type: "open", access: "CodeBuddy Code · Claude Code", real: true  },
];

window.WB_TRACKS = [
  { name: "WorkBuddyBench-Code",     short: "Code",     metric: "Reward / Hidden-test" },
  { name: "WorkBuddyBench-Web",      short: "Web",      metric: "Rubric-item" },
  { name: "WorkBuddyBench-Office",   short: "Office",   metric: "Rubric" },
  // preview:true is an internal flag that excludes Security from the cross-track
  // Average (Security uses security-specific red/blue scoring). It is NOT a
  // preview status — Security is the official/current version. The key name is
  // kept as-is to avoid touching the Avg-exclusion logic in app_v2.js.
  { name: "WorkBuddyBench-Security", short: "Security", metric: "Red/blue security", preview: true },
];
