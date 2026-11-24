# Mitigating rising Windows commit charge while using Codex Desktop

[简体中文](README.md) · **English**

If Task Manager’s **Committed** value keeps climbing while the Microsoft Store edition of Codex Desktop is running, check which allocations are growing. On one Windows machine, a junction in the desktop profile path was associated with sustained growth in NTFS nonpaged pool allocations tagged `NtFC`. Moving the profile back to the package’s own directory while Codex was closed slowed that growth. This guide explains how to identify the same layout, decide whether the workaround applies, and roll it back.

**What changed on that machine?** `NtFC` grew by about **6.83 GiB in one hour** with the old layout and by **0.197 MiB in the first hour** after the change. About **15.24 GiB** already allocated to the tag remained resident. Those figures measure the pool tag. The change in total system commit can be different.

## Check whether this case fits

1. Under your ordinary workload, record Windows Task Manager → Performance → Memory → **Committed**, the nonpaged pool total, and the relevant processes’ **Private Bytes** at two or more times. Private Bytes measures private committed memory for a process. Its **working set** measures resident pages and can include shared memory.
2. Check the **Bytes** column for the `NtFC` nonpaged tag in Microsoft PoolMon over the same interval. Windows can retain kernel allocations even after an application’s private memory and working set fall. A single peak in system commit does not identify the allocation source. [Microsoft PoolMon documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/poolmon) · [Process memory counters](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters_ex)
3. Run the layout checker below. Consider the offline workaround only if `NtFC` is growing under ordinary conditions **and** the checker returns `match`. If commit rises while the tag stays flat, investigate other applications and allocations.

## Run the read-only checker

On Windows 10 or 11, open a terminal in the extracted directory as the current user. The checker uses Windows PowerShell 5.1 and Python 3.11 or later with the standard library. [Download v0.1.0](https://github.com/JingWang-Star996/codex-windows-commit-diagnostics/releases/tag/v0.1.0)

```powershell
python -I -B diagnose.py
python -I -B diagnose.py --plan
```

Both commands print JSON. The checker verifies the current user’s registered Store package and reads directory metadata at two fixed locations. It does not read profile contents or make changes. With `--plan`, a matching layout also prints the offline procedure in outline.

| `layout` | What it establishes | What to do |
|---|---|---|
| `match` | The private name is a junction pointing directly to the ordinary public profile directory. | Confirm the `NtFC` trend and the offline prerequisites below. |
| `native` | The public name is absent and the private profile is an ordinary directory. | This is consistent with the observed post-move layout. It cannot establish how the machine reached that state. |
| `indeterminate` | The package, permissions, volume, directory metadata or repeated observations did not establish either layout. | Leave the layout in place and investigate separately. |

Exit code 0 means the checker classified the layout. Exit code 2 means it could not classify the layout or received an invalid argument. The Windows fixture suite runs separately with `python -I -B tests.py`. Its 20 tests exercise detection and the manual move sequence using temporary directories.

## What the before-and-after measurements show

On November 19, 2026, the packaged desktop application was retrying failed profile-file renames. After the profile was moved offline, the application was reopened once. The same machine yielded these observations.

| Observation window | Change in nonpaged bytes tagged `NtFC` |
|---|---|
| Historical one-hour window, old layout | About +6.83 GiB |
| First hour after reopening | About +0.197 MiB |
| From reopening through 2 h 16 min | Still about +0.197 MiB in total |
| Following morning, compared with the previous evening | +8,320 bytes |

Total system commit rose again the next day while the tag barely changed. The evidence supports this workaround for the observed layout on one machine. It does not establish an upstream code defect or the effect on other machines or versions.

## Offline workaround for the matching layout

Proceed only if the checker returns `match`, the measurements fit, and you can independently verify object identities and restore a backup. The observations cover one local NTFS volume and the current user’s Store installation. Shared machines, redirected user profiles, cross-volume moves, other installation channels and other junction topologies were outside the verification.

| Name | Required starting state |
|---|---|
| **P** — public profile name | `%APPDATA%\Codex` is the ordinary directory containing the current profile. |
| **Q** — package-private profile name | `%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Roaming\Codex` is a directory junction pointing directly to P. |
| **R** — name for the retained junction | A previously unused sibling of Q. A dangling junction counts as an occupied name. |

The variables above are shorthand. The checker uses the current user’s Windows Known Folder locations for RoamingAppData and LocalAppData. Verify those locations outside the package. Stop if the paths or the package’s redirected view disagree with the layout you have identified.

1. Save your work and quit Codex Desktop and every helper that uses the profile. Check that they stay closed through both renames. Make a complete, restorable backup outside P, Q and R. Protect the personal data in that backup.
2. From outside the package, record the package identity, the volume and file ID of P, the volume and file ID of the **junction itself** at Q, its direct target, and the chosen R. Verify that both paths have ordinary local directories as ancestors and that their parents are on the same NTFS volume. A matching drive letter alone is insufficient.
3. Rename **the junction itself** from Q to R in the same parent directory. Confirm that R is still the original junction pointing to P, Q is vacant, and P’s identity is unchanged.
4. Move **the ordinary directory** P to the vacant name Q using a same-volume rename. Confirm that P is vacant and Q is an ordinary directory with the original file ID of P. The retained junction R now points to the vacant old name. Cross-volume copying, overwriting or merging breaks the rollback procedure.
5. Reopen Codex manually. Verify existing data and new writes. Compare the `NtFC` delta at startup and after about an hour of ordinary use alongside system and process counters. The checker should now report `native`. Keep the backup, R and the recorded identities until you have verified the data and the trend.

### Interruption and rollback

| State you can verify | Action |
|---|---|
| Only Q → R completed. P is the original directory, Q is vacant, and R is the original junction. | Keep the application closed. Rename R back to Q and verify the identities and Q’s direct target. |
| Both moves completed. P is vacant, Q has the original directory ID, and R is the original junction. | Close the application and helpers again and back up the **current** contents of Q. Move Q back to P on the same volume, then rename R back to Q. Verify IDs, the target and the post-move writes. |
| P reappeared, an identity changed, a backup is incomplete, objects are missing, or another writer is active. | Preserve the objects and latest data. Reconcile the recorded identities and backup before taking further action. |

A `native` result covers P and Q only. Rollback also requires the retained junction, the original records and a verified backup.

## Related upstream reports

- [#58641](https://github.com/openai/codex/issues/58641) describes `NtFC` growth with redirected AppData and improvement after a layout change. Its original junctions had different targets, so the move sequence above does not apply directly.
- [#55812](https://github.com/openai/codex/issues/55812) reports that `NtFC` stopped growing after Codex exited while earlier allocations remained resident. It does not establish this particular junction layout.
- [#31212](https://github.com/openai/codex/issues/31212) reports growth across several kernel pool tags. In [#38765](https://github.com/openai/codex/issues/38765), Windows events attributed substantial commit directly to `codex.exe`.
- [#63592](https://github.com/openai/codex/issues/63592) reports a process tree reaching about 6 GB during image-queue retries. The memory counter was unspecified, so a shared cause has not been established.
