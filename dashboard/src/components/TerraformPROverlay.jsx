/**
 * TerraformPROverlay.jsx — 2-step modal for creating Terraform PRs.
 *
 * Step 1 — Repo + path (existing behaviour, unchanged):
 *   User confirms the GitHub repo and Terraform path, then clicks "Analyse →".
 *   Calls POST /api/execution/{id}/resolve-tf-change to locate the TF block
 *   and propose an attribute:value change.
 *
 * Step 2 — Change preview (new):
 *   Shows the located block address, current attribute value, and proposed new
 *   value (editable).  If the block couldn't be found, warns the user that a
 *   stub PR will be created instead.  Human clicks "Create PR".
 *
 * For MODIFY_NSG actions the overlay skips Step 2 (block + patch are already
 * handled deterministically by the PR generator) and goes straight to PR creation.
 *
 * Props:
 *   executionId    — UUID of the ExecutionRecord
 *   actionType     — action_type from the verdict (e.g. "update_config")
 *   detectedRepo   — iac_repo from the execution record (may be empty)
 *   detectedPath   — iac_path from the execution record (may be empty)
 *   onConfirm(iacRepo, iacPath, confirmedChange) — called when user confirms
 *   onCancel()     — called when user clicks Cancel or presses Escape
 *   loading        — true while the parent is submitting (disables buttons)
 */

import React, { useEffect, useRef, useState } from 'react'
import { fetchGithubRepos, resolveTfChange } from '../api'

// Action types where block-finding + change confirmation adds value.
// MODIFY_NSG is excluded — it's handled fully deterministically.
const RESOLVE_SUPPORTED = new Set([
  'update_config',
  'scale_up',
  'scale_down',
])

const CONFIDENCE_LABEL = {
  high: { text: 'High confidence', cls: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30' },
  medium: { text: 'Medium confidence', cls: 'text-amber-400 bg-amber-500/10 border-amber-500/30' },
  llm_assisted: { text: 'AI assisted', cls: 'text-blue-400 bg-blue-500/10 border-blue-500/30' },
  low: { text: 'Low confidence', cls: 'text-rose-400 bg-rose-500/10 border-rose-500/30' },
}

export default function TerraformPROverlay({
  executionId = '',
  actionType = '',
  detectedRepo = '',
  detectedPath = '',
  onConfirm,
  onCancel,
  loading = false,
}) {
  // ── Step 1 state ────────────────────────────────────────────────────────
  const [selectedRepo, setSelectedRepo] = useState(detectedRepo)
  const [repoSearch, setRepoSearch]     = useState(detectedRepo)
  const [iacPath, setIacPath]           = useState(detectedPath)
  const [repos, setRepos]               = useState([])
  const [reposLoading, setReposLoading] = useState(false)
  const [reposError, setReposError]     = useState(null)
  const [dropdownOpen, setDropdownOpen] = useState(false)

  // ── Step 2 state ────────────────────────────────────────────────────────
  const [step, setStep]                   = useState(1)
  const [resolving, setResolving]         = useState(false)
  const [resolveError, setResolveError]   = useState(null)
  const [resolveResult, setResolveResult] = useState(null)  // API response
  const [editedValue, setEditedValue]     = useState('')    // human-editable proposed value

  const searchRef   = useRef(null)
  const dropdownRef = useRef(null)

  // Determines whether we show Step 2 at all for this action type
  const needsResolve = RESOLVE_SUPPORTED.has(actionType)

  // Load repo list on mount
  useEffect(() => {
    setReposLoading(true)
    fetchGithubRepos()
      .then(data => setRepos(data.repos ?? []))
      .catch(err => setReposError(err.message))
      .finally(() => setReposLoading(false))
  }, [])

  // Close dropdown on outside click
  useEffect(() => {
    function handler(e) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target))
        setDropdownOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Close on Escape
  useEffect(() => {
    function handler(e) { if (e.key === 'Escape') onCancel() }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onCancel])

  const filtered = repos.filter(r => r.toLowerCase().includes(repoSearch.toLowerCase()))

  function selectRepo(repo) {
    setSelectedRepo(repo)
    setRepoSearch(repo)
    setDropdownOpen(false)
  }

  function handleSearchChange(e) {
    setRepoSearch(e.target.value)
    setSelectedRepo('')
    setDropdownOpen(true)
  }

  // ── Step 1 → "Analyse" button ───────────────────────────────────────────
  async function handleAnalyse() {
    const repo = selectedRepo || repoSearch.trim()
    if (!repo) return

    if (!needsResolve) {
      // MODIFY_NSG and other action types skip directly to PR creation
      onConfirm(repo, iacPath.trim(), null)
      return
    }

    setResolving(true)
    setResolveError(null)
    setResolveResult(null)
    try {
      const result = await resolveTfChange(executionId, repo, iacPath.trim())
      setResolveResult(result)
      setEditedValue(result.proposed_value || '')
      setStep(2)
    } catch (err) {
      setResolveError(err.message)
    } finally {
      setResolving(false)
    }
  }

  // ── Step 2 → "Create PR" button ─────────────────────────────────────────
  function handleConfirmChange() {
    const repo = selectedRepo || repoSearch.trim()
    let confirmedChange = null

    if (resolveResult?.found && resolveResult.attribute) {
      confirmedChange = {
        tf_block_address: resolveResult.tf_block_address,
        file_path: resolveResult.file_path,
        file_sha: resolveResult.file_sha,
        attribute: resolveResult.attribute,
        new_value: editedValue.trim(),
      }
    }
    // If not found, confirmedChange stays null → stub PR fallback in generator
    onConfirm(repo, iacPath.trim(), confirmedChange)
  }

  const canAnalyse = !loading && !resolving && (selectedRepo || repoSearch.trim())
  const canCreatePR = !loading && editedValue.trim()

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onCancel() }}
    >
      <div className="w-full max-w-lg mx-4 bg-slate-900 border border-slate-700/60 rounded-xl shadow-2xl shadow-black/50 p-6 space-y-5">

        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-base font-semibold text-slate-100">Create Terraform PR</h2>
            <p className="mt-0.5 text-xs text-slate-400">
              {step === 1
                ? 'Confirm the target repo and path, then analyse.'
                : 'Review the proposed change before creating the PR.'}
            </p>
          </div>
          <button
            onClick={onCancel}
            className="text-slate-500 hover:text-slate-300 transition-colors text-lg leading-none mt-0.5"
            aria-label="Close"
          >✕</button>
        </div>

        {/* ── STEP 1 ─────────────────────────────────────────────────────── */}
        {step === 1 && (
          <>
            {/* Detection banner */}
            {(() => {
              const confirmedInList = repos.length > 0 && repos.includes(detectedRepo)
              if (detectedRepo && confirmedInList) return (
                <div className="flex items-start gap-2 text-xs text-emerald-300/80 bg-emerald-500/5 border border-emerald-500/20 rounded-lg px-3 py-2">
                  <span className="mt-0.5">✓</span>
                  <span>Detected <code className="font-mono text-emerald-200">{detectedRepo}</code> from resource tags.</span>
                </div>
              )
              if (detectedRepo && !confirmedInList) return (
                <div className="flex items-start gap-2 text-xs text-amber-300/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
                  <span className="mt-0.5">⚠</span>
                  <span>Detected <code className="font-mono text-amber-200">{detectedRepo}</code> from tags, but not in your GitHub account. Select the correct repo below.</span>
                </div>
              )
              return (
                <div className="flex items-start gap-2 text-xs text-amber-300/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
                  <span className="mt-0.5">⚠</span>
                  <span>No repo detected from resource tags. Select the repo that manages this resource.</span>
                </div>
              )
            })()}

            {/* Repo search / combobox */}
            <div className="space-y-1.5">
              <label className="block text-xs font-medium text-slate-300">
                GitHub Repository <span className="text-rose-400">*</span>
              </label>
              <div className="relative" ref={dropdownRef}>
                <input
                  ref={searchRef}
                  type="text"
                  value={repoSearch}
                  onChange={handleSearchChange}
                  onFocus={() => setDropdownOpen(true)}
                  placeholder={reposLoading ? 'Loading repos…' : 'Search or type owner/repo'}
                  disabled={loading || resolving}
                  className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 pr-8 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500/40 disabled:opacity-50"
                />
                {!reposLoading && repos.length > 0 && (
                  <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 text-xs" aria-hidden>▾</span>
                )}
                {dropdownOpen && !reposLoading && (
                  <div className="absolute z-10 mt-1 w-full bg-slate-800 border border-slate-600 rounded-lg shadow-xl max-h-52 overflow-y-auto">
                    {reposError ? (
                      <div className="px-3 py-2 text-xs text-rose-400">{reposError}</div>
                    ) : filtered.length === 0 ? (
                      <div className="px-3 py-2 text-xs text-slate-500">
                        {repoSearch ? 'No matching repos — you can still type the full name.' : 'No repos found.'}
                      </div>
                    ) : (
                      filtered.map(repo => (
                        <button
                          key={repo}
                          onMouseDown={e => { e.preventDefault(); selectRepo(repo) }}
                          className={`w-full text-left px-3 py-2 text-sm hover:bg-slate-700 transition-colors ${repo === selectedRepo ? 'text-blue-300 bg-slate-700/60' : 'text-slate-200'}`}
                        >{repo}</button>
                      ))
                    )}
                  </div>
                )}
              </div>
              {reposLoading && (
                <p className="text-xs text-slate-500 flex items-center gap-1.5">
                  <span className="w-3 h-3 border-2 border-slate-400 border-t-transparent rounded-full animate-spin" />
                  Loading repos from GitHub…
                </p>
              )}
              {!reposLoading && reposError && (
                <p className="text-xs text-amber-400/90 flex items-start gap-1.5">
                  <span className="mt-0.5">⚠</span>
                  <span>{reposError} — type the repo name manually above.</span>
                </p>
              )}
              {!reposLoading && !reposError && repos.length > 0 && (
                <p className="text-xs text-slate-500">{repos.length} repo{repos.length !== 1 ? 's' : ''} found.</p>
              )}
            </div>

            {/* Path field */}
            <div className="space-y-1.5">
              <label className="block text-xs font-medium text-slate-300">
                Terraform Path <span className="text-slate-500">(optional)</span>
              </label>
              <input
                type="text"
                value={iacPath}
                onChange={e => setIacPath(e.target.value)}
                placeholder="infrastructure/terraform-core"
                disabled={loading || resolving}
                className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500/40 disabled:opacity-50"
              />
              <p className="text-xs text-slate-500">Subdirectory within the repo. Leave blank to search the root.</p>
            </div>

            {/* Resolve error */}
            {resolveError && (
              <div className="text-xs text-rose-400 bg-rose-500/5 border border-rose-500/20 rounded-lg px-3 py-2">
                ✕ {resolveError}
              </div>
            )}

            {/* Actions */}
            <div className="flex justify-end gap-2 pt-1">
              <button
                onClick={onCancel}
                disabled={loading || resolving}
                className="px-4 py-2 text-sm text-slate-300 hover:text-slate-100 bg-slate-800 hover:bg-slate-700 border border-slate-600 rounded-lg transition-colors disabled:opacity-50"
              >Cancel</button>
              <button
                onClick={handleAnalyse}
                disabled={!canAnalyse}
                className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-blue-100 bg-blue-600/30 hover:bg-blue-600/50 border border-blue-500/50 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {resolving ? (
                  <><span className="w-3.5 h-3.5 border-2 border-blue-300 border-t-transparent rounded-full animate-spin" /> Analysing…</>
                ) : needsResolve ? (
                  <>Analyse →</>
                ) : (
                  <>📝 Create PR</>
                )}
              </button>
            </div>
          </>
        )}

        {/* ── STEP 2 ─────────────────────────────────────────────────────── */}
        {step === 2 && resolveResult && (
          <>
            {resolveResult.found ? (
              <>
                {/* Block found banner */}
                <div className="flex items-start gap-2 text-xs text-emerald-300/80 bg-emerald-500/5 border border-emerald-500/20 rounded-lg px-3 py-2">
                  <span className="mt-0.5">✓</span>
                  <span>
                    Found <code className="font-mono text-emerald-200">{resolveResult.tf_block_address}</code>
                    {' '}in <code className="font-mono text-emerald-200">{resolveResult.file_path?.split('/').pop()}</code>
                  </span>
                  {resolveResult.confidence && (() => {
                    const cfg = CONFIDENCE_LABEL[resolveResult.confidence] ?? CONFIDENCE_LABEL.low
                    return (
                      <span className={`ml-auto shrink-0 px-1.5 py-0.5 rounded border text-[10px] font-medium ${cfg.cls}`}>
                        {cfg.text}
                      </span>
                    )
                  })()}
                </div>

                {/* Attribute change row */}
                {resolveResult.attribute && (
                  <div className="space-y-3">
                    <div className="grid grid-cols-3 gap-3 text-xs">
                      <div className="space-y-1">
                        <p className="text-slate-500 uppercase tracking-wide font-medium">Attribute</p>
                        <code className="block font-mono text-slate-200 bg-slate-800 rounded px-2 py-1.5">
                          {resolveResult.attribute}
                        </code>
                      </div>
                      <div className="space-y-1">
                        <p className="text-slate-500 uppercase tracking-wide font-medium">Current</p>
                        <code className="block font-mono text-slate-400 bg-slate-800 rounded px-2 py-1.5 line-through">
                          {resolveResult.current_value || '—'}
                        </code>
                      </div>
                      <div className="space-y-1">
                        <p className="text-slate-500 uppercase tracking-wide font-medium">Proposed</p>
                        <input
                          type="text"
                          value={editedValue}
                          onChange={e => setEditedValue(e.target.value)}
                          placeholder="new value"
                          className="w-full font-mono bg-slate-800 border border-blue-500/50 rounded px-2 py-1 text-xs text-blue-200 focus:outline-none focus:ring-1 focus:ring-blue-500/40"
                        />
                      </div>
                    </div>
                    <p className="text-xs text-slate-500">Edit the proposed value if needed, then click Create PR.</p>
                  </div>
                )}

                {/* Block preview (collapsible) */}
                {resolveResult.raw_block_preview && (
                  <details className="group">
                    <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300 transition-colors select-none">
                      View Terraform block preview ▸
                    </summary>
                    <pre className="mt-2 text-[10px] font-mono text-slate-400 bg-slate-800/60 border border-slate-700/40 rounded-lg p-3 overflow-x-auto max-h-40 overflow-y-auto whitespace-pre-wrap">
                      {resolveResult.raw_block_preview}
                    </pre>
                  </details>
                )}
              </>
            ) : (
              /* Block not found */
              <div className="flex items-start gap-2 text-xs text-amber-300/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
                <span className="mt-0.5">⚠</span>
                <span>
                  {resolveResult.reason || 'Could not locate the Terraform block.'}{' '}
                  A stub PR will be created instead — apply the change manually.
                </span>
              </div>
            )}

            {/* Actions */}
            <div className="flex justify-between gap-2 pt-1">
              <button
                onClick={() => { setStep(1); setResolveResult(null); setResolveError(null) }}
                disabled={loading}
                className="px-4 py-2 text-sm text-slate-300 hover:text-slate-100 bg-slate-800 hover:bg-slate-700 border border-slate-600 rounded-lg transition-colors disabled:opacity-50"
              >← Back</button>

              <div className="flex gap-2">
                <button
                  onClick={onCancel}
                  disabled={loading}
                  className="px-4 py-2 text-sm text-slate-300 hover:text-slate-100 bg-slate-800 hover:bg-slate-700 border border-slate-600 rounded-lg transition-colors disabled:opacity-50"
                >Cancel</button>
                <button
                  onClick={handleConfirmChange}
                  disabled={loading || (resolveResult.found && resolveResult.attribute && !editedValue.trim())}
                  className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-blue-100 bg-blue-600/30 hover:bg-blue-600/50 border border-blue-500/50 rounded-lg transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {loading ? (
                    <><span className="w-3.5 h-3.5 border-2 border-blue-300 border-t-transparent rounded-full animate-spin" /> Creating PR…</>
                  ) : (
                    <>📝 Create PR</>
                  )}
                </button>
              </div>
            </div>
          </>
        )}

      </div>
    </div>
  )
}
