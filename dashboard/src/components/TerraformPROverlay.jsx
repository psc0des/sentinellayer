/**
 * TerraformPROverlay.jsx — modal for confirming / overriding the Terraform PR target.
 *
 * Shows the auto-detected repo and path from the execution record.
 * The user can search for a different repo from their GitHub PAT's accessible repos,
 * and edit the Terraform path before confirming PR creation.
 *
 * Props:
 *   executionId  — UUID of the ExecutionRecord
 *   detectedRepo — iac_repo from the execution record (may be empty)
 *   detectedPath — iac_path from the execution record (may be empty)
 *   onConfirm(iacRepo, iacPath) — called when user clicks "Create PR"
 *   onCancel()  — called when user clicks Cancel or presses Escape
 *   loading     — true while the parent is submitting (disables buttons)
 */

import React, { useEffect, useRef, useState } from 'react'
import { fetchGithubRepos } from '../api'

export default function TerraformPROverlay({
  detectedRepo = '',
  detectedPath = '',
  onConfirm,
  onCancel,
  loading = false,
}) {
  const [selectedRepo, setSelectedRepo] = useState(detectedRepo)
  const [repoSearch, setRepoSearch]   = useState(detectedRepo)
  const [iacPath, setIacPath]         = useState(detectedPath)
  const [repos, setRepos]             = useState([])
  const [reposLoading, setReposLoading] = useState(false)
  const [reposError, setReposError]   = useState(null)
  const [dropdownOpen, setDropdownOpen] = useState(false)

  const searchRef   = useRef(null)
  const dropdownRef = useRef(null)

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
      if (dropdownRef.current && !dropdownRef.current.contains(e.target)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Close on Escape
  useEffect(() => {
    function handler(e) {
      if (e.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onCancel])

  const filtered = repos.filter(r =>
    r.toLowerCase().includes(repoSearch.toLowerCase())
  )

  function selectRepo(repo) {
    setSelectedRepo(repo)
    setRepoSearch(repo)
    setDropdownOpen(false)
  }

  function handleSearchChange(e) {
    setRepoSearch(e.target.value)
    setSelectedRepo('')   // clear selection until user picks from list
    setDropdownOpen(true)
  }

  function handleConfirm() {
    const repo = selectedRepo || repoSearch.trim()
    if (!repo) return
    onConfirm(repo, iacPath.trim())
  }

  const canSubmit = !loading && (selectedRepo || repoSearch.trim())

  return (
    /* backdrop */
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
              Confirm the target repo and path, or select a different one.
            </p>
          </div>
          <button
            onClick={onCancel}
            className="text-slate-500 hover:text-slate-300 transition-colors text-lg leading-none mt-0.5"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {/* Detection note — green only when detected repo is confirmed in the loaded list */}
        {(() => {
          const confirmedInList = repos.length > 0 && repos.includes(detectedRepo)
          if (detectedRepo && confirmedInList) {
            return (
              <div className="flex items-start gap-2 text-xs text-emerald-300/80 bg-emerald-500/5 border border-emerald-500/20 rounded-lg px-3 py-2">
                <span className="mt-0.5">✓</span>
                <span>
                  Detected <code className="font-mono text-emerald-200">{detectedRepo}</code> from resource tags.
                  Confirm or select a different repo below.
                </span>
              </div>
            )
          }
          if (detectedRepo && !confirmedInList) {
            return (
              <div className="flex items-start gap-2 text-xs text-amber-300/80 bg-amber-500/5 border border-amber-500/20 rounded-lg px-3 py-2">
                <span className="mt-0.5">⚠</span>
                <span>
                  Detected <code className="font-mono text-amber-200">{detectedRepo}</code> from resource tags,
                  but this repo isn't in your GitHub account. Select the correct repo below.
                </span>
              </div>
            )
          }
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
              disabled={loading}
              className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 pr-8 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500/40 disabled:opacity-50"
            />
            {/* Chevron — shows when repos loaded, hints that it's a searchable dropdown */}
            {!reposLoading && repos.length > 0 && (
              <span
                className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 text-xs"
                aria-hidden
              >
                ▾
              </span>
            )}

            {/* dropdown list */}
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
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-slate-700 transition-colors ${
                        repo === selectedRepo ? 'text-blue-300 bg-slate-700/60' : 'text-slate-200'
                      }`}
                    >
                      {repo}
                    </button>
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
            <p className="text-xs text-slate-500">
              {repos.length} repo{repos.length !== 1 ? 's' : ''} found — click the field to search.
            </p>
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
            disabled={loading}
            className="w-full bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500/40 disabled:opacity-50"
          />
          <p className="text-xs text-slate-500">
            Subdirectory within the repo where Terraform files live. Leave blank to search the root.
          </p>
        </div>

        {/* Actions */}
        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-2 text-sm text-slate-300 hover:text-slate-100 bg-slate-800 hover:bg-slate-700 border border-slate-600 rounded-lg transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            disabled={!canSubmit}
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
    </div>
  )
}
