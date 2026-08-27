interface DashboardCardProps {
  title: string
  value: string
  loading?: boolean
  className?: string
}

export function DashboardCard({ title, value, loading, className = "" }: DashboardCardProps) {
  return (
    <div className="dashboard-card">
      <p className="stat-label">{title}</p>
      {loading ? (
        <div className="h-8 bg-muted rounded animate-pulse mt-2" />
      ) : (
        <p className={`stat-value mt-1 ${className}`}>{value}</p>
      )}
    </div>
  )
}
