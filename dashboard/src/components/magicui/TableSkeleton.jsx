/**
 * TableSkeleton — animated skeleton placeholder for table rows.
 *
 * Renders `rows` skeleton rows, each with `cols` cells of varying widths.
 * Uses the `.shimmer` animation from index.css.
 *
 * Props:
 *   rows — number of skeleton rows (default 3)
 *   cols — number of columns (default 5)
 */
export default function TableSkeleton({ rows = 3, cols = 5 }) {
  // Cycle through a few widths so columns look natural, not identical
  const widths = ['w-20', 'w-16', 'w-24', 'w-12', 'w-20', 'w-14', 'w-10', 'w-16', 'w-8']

  return (
    <tbody>
      {Array.from({ length: rows }).map((_, r) => (
        <tr key={r} className="border-b border-slate-800/60 last:border-0">
          {Array.from({ length: cols }).map((_, c) => (
            <td key={c} className="py-3.5 pr-4">
              <div
                className={`h-2.5 rounded-full bg-slate-800 shimmer ${widths[(r + c) % widths.length]}`}
              />
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  )
}
