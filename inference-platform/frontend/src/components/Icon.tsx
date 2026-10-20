import type { CSSProperties } from 'react'

const paths = {
  chat: 'M21 11.5a8.5 8.5 0 0 1-8.5 8.5H3l2-5a8.5 8.5 0 1 1 16-3.5ZM8 10h8M8 14h5',
  wave: 'M3 10v4m4-8v12m5-16v20m5-16v12m4-8v4',
  grid: 'M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5',
  audio: 'M14 3H5a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9zm0 0v6h6M8 13v3m4-5v7m4-5v3',
  image: 'M3 3h18v18H3zM3 17l6-6 4 4 3-3 5 5M16 7h.01',
  refresh: 'M20 7a8 8 0 1 0 0 10M20 3v5h-5',
  arrow: 'M4 12h16m-6-6 6 6-6 6',
  check: 'm5 12 4 4L19 6',
  clock: 'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  download: 'M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4',
  copy: 'M9 9h12v12H9zM15 5V3H3v12h2',
  close: 'm6 6 12 12M6 18 18 6',
  info: 'M12 11v6m0-10v.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  chevron: 'm9 5 7 7-7 7',
  server: 'M3 3h18v7H3zm0 11h18v7H3zM7 6h.01M7 17h.01',
  warning: 'M12 3 2 21h20zM12 9v5m0 3v.01',
} as const

export function Icon({ name, size = 20, className, style }: {
  name: keyof typeof paths; size?: number; className?: string; style?: CSSProperties
}) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className} style={style}><path d={paths[name]} /></svg>
}
