// Leaderboard data — WorkBuddyBench Code / Web / Office / Security. Public roster only.
// SOURCE OF TRUTH: the team's aligned leaderboard statistics — 3 runs averaged,
// think mode. Code / Office / Security are on the 2026-07-15 aligned snapshot;
// Web was rescored on the stricter rubric (2026-07-16 refresh).
// Harnesses: CodeBuddy Code (cbc) 2.109.3 and Claude Code (cc) 2.1.187.
// `scores` = cbc; `scores_cc` = cc. Renderers pick the LAST "HIGH confidence"
// row per benchmark (the current scored snapshot); earlier rows are history only.
// HY-3 and HY-3-preview are distinct checkpoints.
// Code v2.3 row additionally carries `scores_judge` (mean LLM-judge score, judge
// model kimi-k2.7-judge, whitebox-v1 schema, 7-dim weighted — reference only, does
// not affect ranking) and `code_dims` (per-task-metadata reward slices: role /
// complexity / surface). Both are recomputed cbc-only from the same 3-run ×
// 80-task wb-bench-code-v2.3-lite trial set behind the `scores` row above
// (missing-trial reward counted as 0, matching the `scores` methodology).
window.LEADERBOARD_DATA = {
  "_meta": {
    "generated": "2026-07-14",
    "last_verified": "2026-07-14"
  },
  "benchmarks": [
    {
      "name": "WorkBuddyBench-Code",
      "open_source": "自建",
      "rows": [
        {
          "dataset_version": "v1.2",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "58.30%",
            "GLM5.1-nothink": "51.70%",
            "GLM4.7-nothink": "48.30%",
            "DeepSeek-v4-flash": "48.30%",
            "HY-3-preview": "50.00%"
          }
        },
        {
          "dataset_version": "v2.1",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "58.70%",
            "GLM5.1-nothink": "48.80%",
            "GLM4.7-nothink": "45.70%",
            "DeepSeek-v4-flash": "43.60%",
            "HY-3-preview": "43.00%"
          }
        },
        {
          "dataset_version": "v2.3",
          "notes": "HIGH confidence",
          "_source": "Aligned leaderboard stats (2026-07-09): run-level score, 3 runs averaged, think mode.",
          // Diagnostic rerun (2026-07-14, same 3-run averaging): HY-3 with
          // cross-turn reasoning passback enabled scored 66.72 on cbc (+3.82)
          // and 68.18 on cc (+1.92). Exposed below as `passback_note` — rendered
          // as a footnote only, never as a leaderboard row.
          "passback_note": { "model": "HY-3", "cbc": 66.72, "cbc_delta": "+3.82", "cc": 68.18, "cc_delta": "+1.92" },
          "scores": {
            "Claude Opus 4.8": "74.43%(think)",
            "GPT-5.5": "72.90%(think)",
            "GLM-5.2": "71.54%(think)",
            "GLM5.1-nothink": "63.74%(think)",
            "MiniMax-M3": "60.14%(think)",
            "DeepSeek-V4-Pro": "58.92%(think)",
            "DeepSeek-v4-flash": "55.73%(think)",
            "Kimi K2.6": "55.93%(think)",
            "Kimi K2.7": "63.42%(think)",
            "HY-3": "62.90%(think)",
            "HY-3-preview": "53.57%(think)"
          },
          "scores_cc": {
            // Claude Opus 4.8's cc Code score comes from a modified-instruction run:
            // AskUserQuestion disabled plus an explicit no-ask / complete-in-one-pass
            // instruction, so its setup differs slightly from the other models' runs.
            "Claude Opus 4.8": "77.90%(think)",
            "GLM-5.2": "77.06%(think)",
            "GPT-5.5": "76.63%(think)",
            "GLM5.1-nothink": "67.06%(think)",
            "MiniMax-M3": "66.42%(think)",
            "HY-3": "66.26%(think)",
            "DeepSeek-V4-Pro": "64.59%(think)",
            "DeepSeek-v4-flash": "61.89%(think)",
            "HY-3-preview": "57.71%(think)"
          },
          "scores_judge": {
            "Claude Opus 4.8": "76.32%(think)",
            "GPT-5.5": "77.11%(think)",
            "GLM-5.2": "73.80%(think)",
            "GLM5.1-nothink": "68.89%(think)",
            "MiniMax-M3": "60.08%(think)",
            "DeepSeek-V4-Pro": "64.47%(think)",
            "DeepSeek-v4-flash": "60.25%(think)",
            "Kimi K2.6": "65.86%(think)",
            "Kimi K2.7": "67.23%(think)",
            "HY-3": "66.49%(think)",
            "HY-3-preview": "61.49%(think)"
          },
          "code_dims": {
            "role": {
              "developer": {
                "Claude Opus 4.8": "68.81%(think)",
                "GPT-5.5": "67.11%(think)",
                "GLM-5.2": "66.75%(think)",
                "GLM5.1-nothink": "56.87%(think)",
                "MiniMax-M3": "46.76%(think)",
                "DeepSeek-V4-Pro": "47.17%(think)",
                "DeepSeek-v4-flash": "52.81%(think)",
                "Kimi K2.6": "56.91%(think)",
                "Kimi K2.7": "59.45%(think)",
                "HY-3": "56.12%(think)",
                "HY-3-preview": "47.12%(think)"
              },
              "algo": {
                "Claude Opus 4.8": "81.05%(think)",
                "GPT-5.5": "84.31%(think)",
                "GLM-5.2": "72.64%(think)",
                "GLM5.1-nothink": "76.30%(think)",
                "MiniMax-M3": "69.27%(think)",
                "DeepSeek-V4-Pro": "68.88%(think)",
                "DeepSeek-v4-flash": "62.02%(think)",
                "Kimi K2.6": "60.43%(think)",
                "Kimi K2.7": "66.07%(think)",
                "HY-3": "76.43%(think)",
                "HY-3-preview": "67.40%(think)"
              },
              "pm": {
                "Claude Opus 4.8": "84.58%(think)",
                "GPT-5.5": "70.34%(think)",
                "GLM-5.2": "83.56%(think)",
                "GLM5.1-nothink": "59.25%(think)",
                "MiniMax-M3": "71.54%(think)",
                "DeepSeek-V4-Pro": "64.03%(think)",
                "DeepSeek-v4-flash": "56.38%(think)",
                "Kimi K2.6": "45.59%(think)",
                "Kimi K2.7": "61.51%(think)",
                "HY-3": "61.67%(think)",
                "HY-3-preview": "43.21%(think)"
              },
              "qa": {
                "Claude Opus 4.8": "74.82%(think)",
                "GPT-5.5": "78.40%(think)",
                "GLM-5.2": "88.18%(think)",
                "GLM5.1-nothink": "78.25%(think)",
                "MiniMax-M3": "77.92%(think)",
                "DeepSeek-V4-Pro": "80.25%(think)",
                "DeepSeek-v4-flash": "74.08%(think)",
                "Kimi K2.6": "68.86%(think)",
                "Kimi K2.7": "77.81%(think)",
                "HY-3": "72.67%(think)",
                "HY-3-preview": "75.60%(think)"
              },
              "ops": {
                "Claude Opus 4.8": "63.25%(think)",
                "GPT-5.5": "69.14%(think)",
                "GLM-5.2": "55.82%(think)",
                "GLM5.1-nothink": "58.54%(think)",
                "MiniMax-M3": "55.17%(think)",
                "DeepSeek-V4-Pro": "54.78%(think)",
                "DeepSeek-v4-flash": "40.56%(think)",
                "Kimi K2.6": "52.19%(think)",
                "Kimi K2.7": "61.06%(think)",
                "HY-3": "53.47%(think)",
                "HY-3-preview": "48.93%(think)"
              }
            },
            "complexity": {
              "L2": {
                "Claude Opus 4.8": "52.98%(think)",
                "GPT-5.5": "62.80%(think)",
                "GLM-5.2": "50.89%(think)",
                "GLM5.1-nothink": "48.51%(think)",
                "MiniMax-M3": "34.92%(think)",
                "DeepSeek-V4-Pro": "43.95%(think)",
                "DeepSeek-v4-flash": "30.06%(think)",
                "Kimi K2.6": "60.42%(think)",
                "Kimi K2.7": "43.95%(think)",
                "HY-3": "48.81%(think)",
                "HY-3-preview": "37.00%(think)"
              },
              "L3": {
                "Claude Opus 4.8": "71.49%(think)",
                "GPT-5.5": "70.95%(think)",
                "GLM-5.2": "68.88%(think)",
                "GLM5.1-nothink": "64.19%(think)",
                "MiniMax-M3": "60.25%(think)",
                "DeepSeek-V4-Pro": "59.41%(think)",
                "DeepSeek-v4-flash": "58.08%(think)",
                "Kimi K2.6": "55.61%(think)",
                "Kimi K2.7": "69.05%(think)",
                "HY-3": "59.65%(think)",
                "HY-3-preview": "54.48%(think)"
              },
              "L4": {
                "Claude Opus 4.8": "81.67%(think)",
                "GPT-5.5": "76.50%(think)",
                "GLM-5.2": "76.08%(think)",
                "GLM5.1-nothink": "67.72%(think)",
                "MiniMax-M3": "66.18%(think)",
                "DeepSeek-V4-Pro": "61.43%(think)",
                "DeepSeek-v4-flash": "58.44%(think)",
                "Kimi K2.6": "55.61%(think)",
                "Kimi K2.7": "62.20%(think)",
                "HY-3": "70.20%(think)",
                "HY-3-preview": "57.09%(think)"
              },
              "L5": {
                "Claude Opus 4.8": "60.62%(think)",
                "GPT-5.5": "67.25%(think)",
                "GLM-5.2": "68.56%(think)",
                "GLM5.1-nothink": "51.49%(think)",
                "MiniMax-M3": "44.17%(think)",
                "DeepSeek-V4-Pro": "52.95%(think)",
                "DeepSeek-v4-flash": "48.01%(think)",
                "Kimi K2.6": "56.34%(think)",
                "Kimi K2.7": "56.78%(think)",
                "HY-3": "46.42%(think)",
                "HY-3-preview": "42.55%(think)"
              }
            },
            "surface": {
              "single_file": {
                "Claude Opus 4.8": "63.78%(think)",
                "GPT-5.5": "44.38%(think)",
                "GLM-5.2": "45.03%(think)",
                "GLM5.1-nothink": "31.34%(think)",
                "MiniMax-M3": "31.07%(think)",
                "DeepSeek-V4-Pro": "38.78%(think)",
                "DeepSeek-v4-flash": "37.32%(think)",
                "Kimi K2.6": "51.67%(think)",
                "Kimi K2.7": "52.11%(think)",
                "HY-3": "33.57%(think)",
                "HY-3-preview": "33.43%(think)"
              },
              "multi_file": {
                "Claude Opus 4.8": "62.90%(think)",
                "GPT-5.5": "73.86%(think)",
                "GLM-5.2": "59.78%(think)",
                "GLM5.1-nothink": "54.87%(think)",
                "MiniMax-M3": "38.37%(think)",
                "DeepSeek-V4-Pro": "41.75%(think)",
                "DeepSeek-v4-flash": "37.44%(think)",
                "Kimi K2.6": "50.37%(think)",
                "Kimi K2.7": "49.22%(think)",
                "HY-3": "59.27%(think)",
                "HY-3-preview": "34.83%(think)"
              },
              "cross_module": {
                "Claude Opus 4.8": "60.98%(think)",
                "GPT-5.5": "44.32%(think)",
                "GLM-5.2": "48.48%(think)",
                "GLM5.1-nothink": "27.65%(think)",
                "MiniMax-M3": "54.73%(think)",
                "DeepSeek-V4-Pro": "35.98%(think)",
                "DeepSeek-v4-flash": "21.21%(think)",
                "Kimi K2.6": "38.07%(think)",
                "Kimi K2.7": "50.38%(think)",
                "HY-3": "29.73%(think)",
                "HY-3-preview": "27.65%(think)"
              },
              "partial_repo": {
                "Claude Opus 4.8": "71.48%(think)",
                "GPT-5.5": "80.37%(think)",
                "GLM-5.2": "76.80%(think)",
                "GLM5.1-nothink": "72.56%(think)",
                "MiniMax-M3": "61.27%(think)",
                "DeepSeek-V4-Pro": "60.80%(think)",
                "DeepSeek-v4-flash": "63.52%(think)",
                "Kimi K2.6": "66.90%(think)",
                "Kimi K2.7": "72.35%(think)",
                "HY-3": "68.90%(think)",
                "HY-3-preview": "66.95%(think)"
              },
              "whole_repo": {
                "Claude Opus 4.8": "82.03%(think)",
                "GPT-5.5": "74.64%(think)",
                "GLM-5.2": "73.30%(think)",
                "GLM5.1-nothink": "86.11%(think)",
                "MiniMax-M3": "71.21%(think)",
                "DeepSeek-V4-Pro": "77.52%(think)",
                "DeepSeek-v4-flash": "69.45%(think)",
                "Kimi K2.6": "58.14%(think)",
                "Kimi K2.7": "73.76%(think)",
                "HY-3": "73.56%(think)",
                "HY-3-preview": "76.47%(think)"
              },
              "synthetic_workspace": {
                "Claude Opus 4.8": "88.91%(think)",
                "GPT-5.5": "76.85%(think)",
                "GLM-5.2": "82.76%(think)",
                "GLM5.1-nothink": "69.22%(think)",
                "MiniMax-M3": "77.93%(think)",
                "DeepSeek-V4-Pro": "72.10%(think)",
                "DeepSeek-v4-flash": "63.18%(think)",
                "Kimi K2.6": "46.89%(think)",
                "Kimi K2.7": "59.92%(think)",
                "HY-3": "70.56%(think)",
                "HY-3-preview": "50.49%(think)"
              }
            }
          }
        }
      ]
    },
    {
      "name": "WorkBuddyBench-Web",
      "open_source": "自建",
      "rows": [
        {
          "dataset_version": "v3.0",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "96.54%",
            "GLM5.1-nothink": "92.37%",
            "GLM4.7-nothink": "90.95%",
            "DeepSeek-v4-flash": "82.83%",
            "HY-3-preview": "NA"
          }
        },
        {
          "dataset_version": "v3.1",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "88.9%(nothink)",
            "GLM5.1-nothink": "88.18%",
            "GLM4.7-nothink": "85.72%",
            "DeepSeek-v4-flash": "82.85%(nothink)",
            "HY-3-preview": "78.93%(nothink)"
          }
        },
        {
          "dataset_version": "v3.2",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "85.44%(nothink)",
            "GPT-5.5": "82.51%(nothink)",
            "MiniMax-M3": "81.07%(think)",
            "GLM5.1-nothink": "78.47%(nothink)",
            "GLM4.7-nothink": "78.45%(nothink)",
            "Kimi K2.6": "78.13%(nothink)",
            "DeepSeek-V4-Pro": "75.32%(nothink)",
            "MiniMax-M2.7": "74.71%(think)",
            "DeepSeek-v4-flash": "74.60%(nothink)",
            "HY-3-preview": "70.14%(nothink)"
          }
        },
        {
          "dataset_version": "current",
          "notes": "HIGH confidence",
          "_source": "WorkBuddyBench-Web current 70-task results, 3 runs averaged, think mode.",
          "scores": {
            "Claude Opus 4.8": "68.14%(think)",
            "HY-3": "67.71%(think)",
            "GLM-5.2": "67.43%(think)",
            "GPT-5.5": "61.14%(think)",
            "MiniMax-M3": "58.00%(think)",
            "DeepSeek-V4-Pro": "54.57%(think)",
            "Kimi K2.7": "54.29%(think)",
            "DeepSeek-v4-flash": "47.29%(think)"
          },
          "scores_cc": {
            "Claude Opus 4.8": "69.86%(think)",
            "HY-3": "66.43%(think)",
            "GPT-5.5": "64.86%(think)",
            "GLM-5.2": "60.71%(think)",
            "MiniMax-M3": "52.57%(think)",
            "DeepSeek-V4-Pro": "51.57%(think)",
            "DeepSeek-v4-flash": "50.29%(think)"
          }
        }
      ]
    },
    {
      "name": "WorkBuddyBench-Office",
      "open_source": "自建",
      "rows": [
        {
          "dataset_version": "v1",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "84.90%",
            "GLM5.1-nothink": "86.20%",
            "GLM4.7-nothink": "82.20%",
            "DeepSeek-v4-flash": "88.60%",
            "HY-3-preview": "73.00%"
          }
        },
        {
          "dataset_version": "v1.2",
          "notes": "HIGH confidence",
          "scores": {
            "Claude Opus 4.7": "92.51%(think) / 91.05%(nothink)",
            "DeepSeek-V4-Pro": "87.63%(think)",
            "DeepSeek-v4-flash": "86.13%(think) / 85.62%(nothink)",
            "GLM5.1-nothink": "85.77%",
            "MiniMax-M3": "85.11%(think)",
            "GLM4.7-nothink": "84.47%",
            "HY-3-preview": "80.08%(think) / 68.89%(nothink)"
          }
        },
        {
          "dataset_version": "current",
          "notes": "HIGH confidence",
          "_source": "Aligned leaderboard stats (2026-07-15): 50-case office set, 3 runs averaged, think mode, dual harness.",
          "scores": {
            "Claude Opus 4.8": "82.37%(think)",
            "HY-3": "82.08%(think)",
            "GPT-5.5": "81.96%(think)",
            "Kimi K2.7": "80.88%(think)",
            "GLM-5.2": "79.60%(think)",
            "DeepSeek-V4-Pro": "79.11%(think)",
            "Kimi K2.6": "78.98%(think)",
            "MiniMax-M3": "78.28%(think)",
            "DeepSeek-v4-flash": "77.47%(think)",
            "HY-3-preview": "76.44%(think)"
          },
          // HY-3 breakdown matches the 2026-07-14 CBC rerun above.
          "score_breakdown": {
            "rule_checks": {
              "Claude Opus 4.8": "82.03%",
              "HY-3": "81.19%",
              "GPT-5.5": "81.59%",
              "Kimi K2.7": "80.60%",
              "GLM-5.2": "79.25%",
              "DeepSeek-V4-Pro": "78.92%",
              "Kimi K2.6": "79.35%",
              "MiniMax-M3": "78.35%",
              "DeepSeek-v4-flash": "77.41%",
              "HY-3-preview": "76.11%"
            },
            "llm_judge": {
              "Claude Opus 4.8": "84.17%",
              "HY-3": "86.32%",
              "GPT-5.5": "83.94%",
              "Kimi K2.7": "82.46%",
              "GLM-5.2": "81.85%",
              "DeepSeek-V4-Pro": "80.41%",
              "Kimi K2.6": "78.31%",
              "MiniMax-M3": "78.42%",
              "DeepSeek-v4-flash": "78.11%",
              "HY-3-preview": "78.83%"
            }
          },
          "scores_cc": {
            "GPT-5.5": "86.05%(think)",
            "Claude Opus 4.8": "83.23%(think)",
            "HY-3": "80.08%(think)",
            "GLM-5.2": "79.57%(think)",
            "DeepSeek-V4-Pro": "78.71%(think)",
            "DeepSeek-v4-flash": "77.54%(think)",
            "MiniMax-M3": "76.30%(think)",
            "HY-3-preview": "76.07%(think)",
            "Kimi K2.6": "67.72%(think)"
          }
        }
      ]
    },
    {
      "name": "WorkBuddyBench-Security",
      "open_source": "自建",
      "rows": [
        {
          "dataset_version": "current",
          "notes": "HIGH confidence",
          "_source": "Aligned leaderboard stats (2026-07-15): 60-task security set, 3 runs averaged, think mode, dual harness.",
          "scores": {
            "GLM-5.2": "76.32%(think)",
            "MiniMax-M3": "74.14%(think)",
            "DeepSeek-V4-Pro": "70.04%(think)",
            "DeepSeek-v4-flash": "67.11%(think)",
            "HY-3": "64.50%(think)",
            "GPT-5.5": "64.39%(think)",
            "Claude Opus 4.8": "64.37%(think)",
            "HY-3-preview": "64.17%(think)",
            "Gemini-3.5-Flash": "59.37%(think)",
            "Kimi K2.6": "54.50%(think)"
          },
          "scores_cc": {
            "GLM-5.2": "80.86%(think)",
            "GPT-5.5": "77.91%(think)",
            "Gemini-3.5-Flash": "76.99%(think)",
            "Claude Opus 4.8": "65.87%(think)",
            "HY-3": "65.59%(think)",
            "MiniMax-M3": "59.30%(think)",
            "DeepSeek-V4-Pro": "58.73%(think)",
            "HY-3-preview": "58.63%(think)",
            "DeepSeek-v4-flash": "53.90%(think)",
            "Kimi K2.6": "43.67%(think)"
          },
          // Model-refusal counts across the 3 runs on the security tasks, per
          // harness. Informational only — renderers may ignore this field.
          "refusals": {
            "cbc": {
              "GLM-5.2": 0,
              "MiniMax-M3": 0,
              "DeepSeek-V4-Pro": 0,
              "DeepSeek-v4-flash": 0,
              "HY-3": 0,
              "GPT-5.5": 2,
              "Claude Opus 4.8": 0,
              "HY-3-preview": 0,
              "Gemini-3.5-Flash": 4,
              "Kimi K2.6": 0
            },
            "cc": {
              "GLM-5.2": 0,
              "GPT-5.5": 0,
              "Gemini-3.5-Flash": 4,
              "Claude Opus 4.8": 13,
              "HY-3": 0,
              "MiniMax-M3": 0,
              "DeepSeek-V4-Pro": 0,
              "HY-3-preview": 0,
              "DeepSeek-v4-flash": 0,
              "Kimi K2.6": 0
            }
          }
        }
      ]
    }
  ]
};

// Token/turn statistics from the team's aligned leaderboard statistics
// (snapshot 2026-07-15), all four subsets, 3-run averages.
// Schema: subsets.<Subset> = { metric, cbc: [entries], cc: [entries] } where each
// entry is { model, score, avgTurns, avgInputK, avgOutputK }; Security entries
// additionally carry cachedInputK and refusals (model-refusal count over 3 runs).
// Notes:
//   - turns = unique assistant messages, main + subagents; input tokens include cache.
//   - Security turn/token stats still use the earlier counting caliber (not yet recomputed).
//   - input-token counting differs across harness configs — never compare input
//     tokens across harnesses; charts must plot OUTPUT tokens only.
// These stats share the same aligned-stats snapshot as the "current"/v2.3 rows in
// LEADERBOARD_DATA above — scores here should match those rows.
window.TOKEN_STATS = {
  "meta": { "runs": 3, "mode": "think",
            "harnesses": { "cbc": "CodeBuddy Code 2.109.3", "cc": "Claude Code 2.1.187" } },
  "subsets": {
    "Code": {
      "metric": "Harbor run-level reward",
      "cbc": [
        { "model": "Claude Opus 4.8",   "score": 74.43, "avgTurns": 29.51, "avgInputK": 928.7,  "avgOutputK": 22.3 },
        { "model": "GPT-5.5",           "score": 72.90, "avgTurns": 26.92, "avgInputK": 753.2,  "avgOutputK": 6.9 },
        { "model": "GLM-5.2",           "score": 71.54, "avgTurns": 33.06, "avgInputK": 861.4,  "avgOutputK": 12.3 },
        { "model": "Kimi K2.7",         "score": 63.42, "avgTurns": 42.20, "avgInputK": 884.2,  "avgOutputK": 8.2 },
        { "model": "HY-3",              "score": 62.90, "avgTurns": 26.02, "avgInputK": 586.8,  "avgOutputK": 9.3 },
        { "model": "MiniMax-M3",        "score": 60.14, "avgTurns": 28.89, "avgInputK": 1021.4, "avgOutputK": 8.5 },
        { "model": "DeepSeek-V4-Pro",   "score": 58.92, "avgTurns": 44.01, "avgInputK": 800.0,  "avgOutputK": 10.2 },
        { "model": "DeepSeek-V4-Flash", "score": 55.73, "avgTurns": 40.30, "avgInputK": 700.5,  "avgOutputK": 9.8 },
        { "model": "HY-3-preview",      "score": 53.57, "avgTurns": 34.65, "avgInputK": 619.6,  "avgOutputK": 9.7 }
      ],
      "cc": [
        // Claude Opus 4.8's cc Code stats come from the same modified-instruction run
        // as its 77.90 score (AskUserQuestion disabled + no-ask / single-pass instruction).
        { "model": "Claude Opus 4.8",   "score": 77.90, "avgTurns": 13.2,  "avgInputK": 646.5,  "avgOutputK": 4.7 },
        { "model": "GLM-5.2",           "score": 77.06, "avgTurns": 33.73, "avgInputK": 1243.3, "avgOutputK": 22.0 },
        { "model": "GPT-5.5",           "score": 76.63, "avgTurns": 30.44, "avgInputK": 696.5,  "avgOutputK": 8.7 },
        { "model": "MiniMax-M3",        "score": 66.42, "avgTurns": 33.90, "avgInputK": 1308.3, "avgOutputK": 10.6 },
        { "model": "HY-3",              "score": 66.26, "avgTurns": 18.07, "avgInputK": 659.2,  "avgOutputK": 13.9 },
        { "model": "DeepSeek-V4-Pro",   "score": 64.59, "avgTurns": 24.20, "avgInputK": 642.3,  "avgOutputK": 23.7 },
        { "model": "DeepSeek-V4-Flash", "score": 61.89, "avgTurns": 23.12, "avgInputK": 771.4,  "avgOutputK": 28.6 },
        { "model": "HY-3-preview",      "score": 57.71, "avgTurns": 28.63, "avgInputK": 897.8,  "avgOutputK": 20.9 }
      ]
    },
    "Web": {
      "metric": "Rubric-item score",
      "cbc": [
        { "model": "Claude Opus 4.8",   "score": 68.14, "avgTurns": 15.27, "avgInputK": 710.21,  "avgOutputK": 16.38 },
        { "model": "HY-3",              "score": 67.71, "avgTurns": 21.19, "avgInputK": 827.45,  "avgOutputK": 22.63 },
        { "model": "GLM-5.2",           "score": 67.43, "avgTurns": 28.37, "avgInputK": 1484.27, "avgOutputK": 33.50 },
        { "model": "GPT-5.5",           "score": 61.14, "avgTurns": 19.41, "avgInputK": 553.05,  "avgOutputK": 13.50 },
        { "model": "MiniMax-M3",        "score": 58.00, "avgTurns": 38.79, "avgInputK": 1908.03, "avgOutputK": 25.35 },
        { "model": "DeepSeek-V4-Pro",   "score": 54.57, "avgTurns": 17.38, "avgInputK": 670.14,  "avgOutputK": 20.35 },
        { "model": "Kimi K2.7",         "score": 54.29, "avgTurns": 24.97, "avgInputK": 1321.52, "avgOutputK": 21.77 },
        { "model": "DeepSeek-V4-Flash", "score": 47.29, "avgTurns": 19.12, "avgInputK": 756.63,  "avgOutputK": 17.97 }
      ],
      "cc": [
        { "model": "Claude Opus 4.8",   "score": 69.86, "avgTurns": 13.05, "avgInputK": 635.48,  "avgOutputK": 15.71 },
        { "model": "HY-3",              "score": 66.43, "avgTurns": 17.76, "avgInputK": 800.93,  "avgOutputK": 28.36 },
        { "model": "GPT-5.5",           "score": 64.86, "avgTurns": 19.19, "avgInputK": 503.03,  "avgOutputK": 15.85 },
        { "model": "GLM-5.2",           "score": 60.71, "avgTurns": 36.72, "avgInputK": 1693.90, "avgOutputK": 39.56 },
        { "model": "MiniMax-M3",        "score": 52.57, "avgTurns": 38.30, "avgInputK": 2115.78, "avgOutputK": 28.47 },
        { "model": "DeepSeek-V4-Pro",   "score": 51.57, "avgTurns": 14.70, "avgInputK": 487.86,  "avgOutputK": 36.02 },
        { "model": "DeepSeek-V4-Flash", "score": 50.29, "avgTurns": 16.16, "avgInputK": 595.61,  "avgOutputK": 36.04 }
      ]
    },
    "Office": {
      "metric": "Rubric score",
      "cbc": [
        { "model": "Claude Opus 4.8",   "score": 82.37, "avgTurns": 19.49, "avgInputK": 1059.7, "avgOutputK": 13.6 },
        { "model": "HY-3",              "score": 82.08, "avgTurns": 29.25, "avgInputK": 924.0,  "avgOutputK": 25.6 },
        { "model": "GPT-5.5",           "score": 81.96, "avgTurns": 17.12, "avgInputK": 571.6,  "avgOutputK": 10.2 },
        { "model": "Kimi K2.7",         "score": 80.88, "avgTurns": 38.61, "avgInputK": 1170.9, "avgOutputK": 20.0 },
        { "model": "GLM-5.2",           "score": 79.60, "avgTurns": 27.32, "avgInputK": 936.5,  "avgOutputK": 28.2 },
        { "model": "DeepSeek-V4-Pro",   "score": 79.11, "avgTurns": 41.63, "avgInputK": 1070.0, "avgOutputK": 22.0 },
        { "model": "Kimi K2.6",         "score": 78.98, "avgTurns": 36.67, "avgInputK": 1023.2, "avgOutputK": 21.6 },
        { "model": "MiniMax-M3",        "score": 78.28, "avgTurns": 27.83, "avgInputK": 1313.9, "avgOutputK": 21.1 },
        { "model": "DeepSeek-V4-Flash", "score": 77.47, "avgTurns": 37.22, "avgInputK": 1054.3, "avgOutputK": 20.5 },
        { "model": "HY-3-preview",      "score": 76.44, "avgTurns": 24.13, "avgInputK": 1181.5, "avgOutputK": 20.2 }
      ],
      "cc": [
        { "model": "GPT-5.5",           "score": 86.05, "avgTurns": 19.01, "avgInputK": 514.0, "avgOutputK": 18.4 },
        { "model": "Claude Opus 4.8",   "score": 83.23, "avgTurns": 16.05, "avgInputK": 852.0, "avgOutputK": 14.0 },
        { "model": "HY-3",              "score": 80.08, "avgTurns": 15.69, "avgInputK": 725.6, "avgOutputK": 30.4 },
        { "model": "GLM-5.2",           "score": 79.57, "avgTurns": 24.69, "avgInputK": 621.9, "avgOutputK": 19.5 },
        { "model": "DeepSeek-V4-Pro",   "score": 78.71, "avgTurns": 15.94, "avgInputK": 390.3, "avgOutputK": 24.0 },
        { "model": "DeepSeek-V4-Flash", "score": 77.54, "avgTurns": 16.96, "avgInputK": 430.5, "avgOutputK": 22.7 },
        { "model": "MiniMax-M3",        "score": 76.30, "avgTurns": 26.80, "avgInputK": 721.9, "avgOutputK": 11.9 },
        { "model": "HY-3-preview",      "score": 76.07, "avgTurns": 22.89, "avgInputK": 707.1, "avgOutputK": 18.4 },
        { "model": "Kimi K2.6",         "score": 67.72, "avgTurns": 10.68, "avgInputK": 179.3, "avgOutputK": 15.8 }
      ]
    },
    "Security": {
      "metric": "Deterministic scoring",
      "cbc": [
        { "model": "GLM-5.2",           "score": 76.32, "avgTurns": 60.6,  "avgInputK": 2729.18,  "avgOutputK": 31.00, "cachedInputK": 1079.23, "refusals": 0 },
        { "model": "MiniMax-M3",        "score": 74.14, "avgTurns": 88.8,  "avgInputK": 11086.46, "avgOutputK": 26.12, "cachedInputK": 4588.36, "refusals": 0 },
        { "model": "DeepSeek-V4-Pro",   "score": 70.04, "avgTurns": 70.1,  "avgInputK": 3367.29,  "avgOutputK": 29.77, "cachedInputK": 1334.53, "refusals": 0 },
        { "model": "DeepSeek-V4-Flash", "score": 67.11, "avgTurns": 69.7,  "avgInputK": 3339.06,  "avgOutputK": 24.20, "cachedInputK": 1336.73, "refusals": 0 },
        { "model": "HY-3",              "score": 64.50, "avgTurns": 54.5,  "avgInputK": 3258.86,  "avgOutputK": 24.23, "cachedInputK": 1595.96, "refusals": 0 },
        { "model": "GPT-5.5",           "score": 64.39, "avgTurns": 34.6,  "avgInputK": 1518.18,  "avgOutputK": 7.54,  "cachedInputK": 626.62,  "refusals": 2 },
        { "model": "Claude Opus 4.8",   "score": 64.37, "avgTurns": 32.4,  "avgInputK": 1902.39,  "avgOutputK": 9.99,  "cachedInputK": 768.85,  "refusals": 0 },
        { "model": "HY-3-preview",      "score": 64.17, "avgTurns": 88.6,  "avgInputK": 2945.44,  "avgOutputK": 24.18, "cachedInputK": 1160.89, "refusals": 0 },
        { "model": "Gemini-3.5-Flash",  "score": 59.37, "avgTurns": 38.9,  "avgInputK": 2492.60,  "avgOutputK": 8.24,  "cachedInputK": 56.70,   "refusals": 4 },
        { "model": "Kimi K2.6",         "score": 54.50, "avgTurns": 112.4, "avgInputK": 10030.76, "avgOutputK": 20.77, "cachedInputK": 4354.29, "refusals": 0 }
      ],
      "cc": [
        { "model": "GLM-5.2",           "score": 80.86, "avgTurns": 77.4, "avgInputK": 2255.98, "avgOutputK": 30.31, "cachedInputK": 1912.30, "refusals": 0 },
        { "model": "GPT-5.5",           "score": 77.91, "avgTurns": 29.8, "avgInputK": 695.02,  "avgOutputK": 7.50,  "cachedInputK": 534.74,  "refusals": 0 },
        { "model": "Gemini-3.5-Flash",  "score": 76.99, "avgTurns": 59.8, "avgInputK": 3355.59, "avgOutputK": 15.46, "cachedInputK": 104.63,  "refusals": 4 },
        { "model": "Claude Opus 4.8",   "score": 65.87, "avgTurns": 47.0, "avgInputK": 1149.49, "avgOutputK": 22.89, "cachedInputK": 1149.45, "refusals": 13 },
        { "model": "HY-3",              "score": 65.59, "avgTurns": 44.1, "avgInputK": 922.25,  "avgOutputK": 25.11, "cachedInputK": 707.26,  "refusals": 0 },
        { "model": "MiniMax-M3",        "score": 59.30, "avgTurns": 54.6, "avgInputK": 2211.03, "avgOutputK": 14.24, "cachedInputK": 1841.21, "refusals": 0 },
        { "model": "DeepSeek-V4-Pro",   "score": 58.73, "avgTurns": 56.1, "avgInputK": 987.66,  "avgOutputK": 36.20, "cachedInputK": 828.19,  "refusals": 0 },
        { "model": "HY-3-preview",      "score": 58.63, "avgTurns": 89.9, "avgInputK": 2867.51, "avgOutputK": 31.56, "cachedInputK": 2215.42, "refusals": 0 },
        { "model": "Kimi K2.6",         "score": 43.67, "avgTurns": 22.2, "avgInputK": 394.83,  "avgOutputK": 11.25, "cachedInputK": 344.98,  "refusals": 0 }
      ]
    }
  }
};
