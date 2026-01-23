/**
 * 表格骨架屏组件
 * 
 * 用于在表格加载时显示友好的骨架屏，提升用户体验
 */
import { Skeleton, Card } from 'antd'

interface TableSkeletonProps {
  rows?: number
  columns?: number
}

export function TableSkeleton({ rows = 5, columns = 6 }: TableSkeletonProps) {
  return (
    <Card>
      {/* 表头骨架 */}
      <div style={{ marginBottom: 16 }}>
        <Skeleton.Button active style={{ width: 200, marginRight: 8 }} />
        <Skeleton.Button active style={{ width: 100 }} />
      </div>

      {/* 表格行骨架 */}
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} style={{ marginBottom: 12 }}>
          <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
            {Array.from({ length: columns }).map((_, colIndex) => (
              <Skeleton.Input
                key={colIndex}
                active
                style={{
                  width: colIndex === 0 ? 150 : colIndex === columns - 1 ? 80 : 120,
                  height: 24,
                }}
              />
            ))}
          </div>
        </div>
      ))}

      {/* 分页骨架 */}
      <div style={{ marginTop: 16, display: 'flex', justifyContent: 'flex-end' }}>
        <Skeleton.Button active style={{ width: 300 }} />
      </div>
    </Card>
  )
}
