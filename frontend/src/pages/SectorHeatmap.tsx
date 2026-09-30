import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { type Sector, type SectorSymbol, sectorHeatmapApi } from '@/api/sector-heatmap'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useMarketDataContextOptional } from '@/contexts/MarketDataContext'
import { MarketDataManager, type SymbolData } from '@/lib/MarketDataManager'

/** How often the heatmap re-renders from the live tick store. */
const REFRESH_MS = 1000
/** Colour saturates at this absolute % change, as on NSE's heatmap. */
const COLOR_CAP_PCT = 3

interface Quote {
  ltp: number
  prevClose: number | null
  pct: number | null
}

interface StockRow {
  symbol: string
  name: string
  industry: string
  quote: Quote | undefined
}

interface SectorRow {
  sector: Sector
  stocks: StockRow[]
  avgPct: number | null
  indexPct: number | null
  advancers: number
  decliners: number
  priced: number
}

type SortKey = 'change' | 'name'

function quoteKey(symbol: string, exchange: string): string {
  return `${exchange}:${symbol}`
}

function extractQuote(data: SymbolData): Quote | null {
  const d = data.data
  const ltp = d?.ltp
  if (ltp == null || !(ltp > 0)) return null
  const prevClose = d.close != null && d.close > 0 ? d.close : null
  let pct: number | null = d.change_percent ?? null
  if (pct == null && prevClose != null) pct = ((ltp - prevClose) / prevClose) * 100
  return { ltp, prevClose, pct }
}

/** Red-to-green overlay that reads on both light and dark card backgrounds. */
function tileStyle(pct: number | null | undefined): { background: string; color?: string } {
  if (pct == null || Number.isNaN(pct)) return { background: 'transparent' }
  const strength = Math.min(Math.abs(pct) / COLOR_CAP_PCT, 1)
  const alpha = 0.12 + strength * 0.78
  const rgb = pct > 0 ? '22, 163, 74' : pct < 0 ? '220, 38, 38' : '148, 163, 184'
  return {
    background: `rgba(${rgb}, ${alpha.toFixed(3)})`,
    color: alpha > 0.5 ? '#ffffff' : undefined,
  }
}

function fmtPct(pct: number | null | undefined): string {
  if (pct == null || Number.isNaN(pct)) return '--'
  return `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '--'
  return v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function buildRows(sectors: Sector[], quotes: Map<string, Quote>): SectorRow[] {
  return sectors.map((sector) => {
    const stocks: StockRow[] = sector.stocks.map((s) => ({
      ...s,
      quote: quotes.get(quoteKey(s.symbol, 'NSE')),
    }))
    const pcts = stocks.map((s) => s.quote?.pct).filter((p): p is number => p != null)
    const index = sector.index_symbol
      ? quotes.get(quoteKey(sector.index_symbol, 'NSE_INDEX'))
      : undefined
    return {
      sector,
      stocks,
      avgPct: pcts.length ? pcts.reduce((a, b) => a + b, 0) / pcts.length : null,
      indexPct: index?.pct ?? null,
      advancers: pcts.filter((p) => p > 0).length,
      decliners: pcts.filter((p) => p < 0).length,
      priced: pcts.length,
    }
  })
}

function compareByChange(a: number | null | undefined, b: number | null | undefined): number {
  if (a == null && b == null) return 0
  if (a == null) return 1
  if (b == null) return -1
  return b - a
}

export default function SectorHeatmap() {
  const context = useMarketDataContextOptional()
  const managerRef = useRef<MarketDataManager>(context?.manager ?? MarketDataManager.getInstance())

  const [sectors, setSectors] = useState<Sector[]>([])
  const [symbols, setSymbols] = useState<SectorSymbol[]>([])
  const [generatedAt, setGeneratedAt] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [conn, setConn] = useState({
    isConnected: false,
    isAuthenticated: false,
    isFallbackMode: false,
  })

  const [rows, setRows] = useState<SectorRow[]>([])
  const [lastRender, setLastRender] = useState<Date | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('change')
  const [selected, setSelected] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  // Live tick store: a ref so ticks never re-render the page directly.
  const quotesRef = useRef<Map<string, Quote>>(new Map())
  const sectorsRef = useRef<Sector[]>([])
  useEffect(() => {
    sectorsRef.current = sectors
  }, [sectors])

  const loadUniverse = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await sectorHeatmapApi.getUniverse()
      if (res.status === 'success' && res.data) {
        setSectors(res.data.sectors)
        setSymbols(res.data.symbols)
        setGeneratedAt(res.data.generated_at)
      } else {
        setError(res.message || 'Failed to load the sector lists')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load the sector lists')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadUniverse()
  }, [loadUniverse])

  // Subscribe every constituent and sector index in Quote mode (LTP + previous close).
  useEffect(() => {
    if (symbols.length === 0) return
    const manager = managerRef.current
    manager.setAutoReconnect(true)
    manager.connect()
    const stateUnsub = manager.addStateListener((s) => {
      setConn({
        isConnected: s.isConnected,
        isAuthenticated: s.isAuthenticated,
        isFallbackMode: s.isFallbackMode,
      })
    })
    const unsubs: Array<() => void> = []
    for (const { symbol, exchange } of symbols) {
      const key = quoteKey(symbol, exchange)
      unsubs.push(
        manager.subscribe(symbol, exchange, 'Quote', (data: SymbolData) => {
          const q = extractQuote(data)
          if (q) quotesRef.current.set(key, q)
        })
      )
      const cached = manager.getCachedData(symbol, exchange)
      if (cached) {
        const q = extractQuote(cached)
        if (q) quotesRef.current.set(key, q)
      }
    }
    return () => {
      stateUnsub()
      for (const u of unsubs) u()
    }
  }, [symbols])

  // Throttled rebuild from the tick store.
  useEffect(() => {
    const id = setInterval(() => {
      setRows(buildRows(sectorsRef.current, quotesRef.current))
      setLastRender(new Date())
    }, REFRESH_MS)
    return () => clearInterval(id)
  }, [])

  const sortedSectors = useMemo(() => {
    const out = [...rows]
    if (sortKey === 'name') out.sort((a, b) => a.sector.name.localeCompare(b.sector.name))
    else out.sort((a, b) => compareByChange(a.avgPct, b.avgPct))
    return out
  }, [rows, sortKey])

  const visibleSectors = useMemo(
    () => (selected ? sortedSectors.filter((r) => r.sector.key === selected) : sortedSectors),
    [sortedSectors, selected]
  )

  const needle = search.trim().toUpperCase()
  const pricedTotal = useMemo(() => {
    const seen = new Set<string>()
    for (const r of rows) for (const s of r.stocks) if (s.quote?.pct != null) seen.add(s.symbol)
    return seen.size
  }, [rows])
  const stockTotal = useMemo(
    () => new Set(sectors.flatMap((s) => s.stocks.map((x) => x.symbol))).size,
    [sectors]
  )

  const connBadge = () => {
    if (conn.isFallbackMode) return <Badge variant="secondary">REST fallback (after-hours)</Badge>
    if (conn.isAuthenticated) return <Badge className="bg-emerald-600 text-white">Live</Badge>
    if (conn.isConnected) return <Badge variant="secondary">Authenticating...</Badge>
    return <Badge variant="outline">Connecting...</Badge>
  }

  return (
    <div className="py-6 space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Sector Heatmap</h1>
          <p className="text-muted-foreground mt-1">
            NSE sectoral indices and their constituents, coloured by live change from the previous
            close.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {connBadge()}
          <Button variant="outline" size="sm" onClick={loadUniverse} disabled={loading}>
            {loading ? 'Loading...' : 'Reload lists'}
          </Button>
        </div>
      </div>

      {error && (
        <Card>
          <CardContent className="py-4 text-sm text-destructive">{error}</CardContent>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-3 text-sm">
        <div className="flex items-center gap-1">
          <span className="text-muted-foreground">Sort sectors:</span>
          <Button
            size="sm"
            variant={sortKey === 'change' ? 'default' : 'outline'}
            onClick={() => setSortKey('change')}
          >
            By change
          </Button>
          <Button
            size="sm"
            variant={sortKey === 'name' ? 'default' : 'outline'}
            onClick={() => setSortKey('name')}
          >
            By name
          </Button>
        </div>
        <Input
          className="w-48 h-8"
          placeholder="Find a stock"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {selected && (
          <Button size="sm" variant="outline" onClick={() => setSelected(null)}>
            Show all sectors
          </Button>
        )}
        <span className="text-muted-foreground ml-auto">
          {pricedTotal}/{stockTotal} stocks priced
          {lastRender ? ` | updated ${lastRender.toLocaleTimeString('en-IN')}` : ''}
          {generatedAt
            ? ` | lists from NSE ${new Date(generatedAt).toLocaleDateString('en-IN')}`
            : ''}
        </span>
      </div>

      <Legend />

      {/* Sector strip */}
      <div className="grid gap-2 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
        {sortedSectors.map((r) => {
          const active = selected === r.sector.key
          return (
            <button
              type="button"
              key={r.sector.key}
              onClick={() => setSelected(active ? null : r.sector.key)}
              className={`rounded-md border p-2 text-left transition-shadow hover:shadow-md ${
                active ? 'ring-2 ring-primary' : ''
              }`}
              style={tileStyle(r.avgPct)}
            >
              <div className="text-xs font-semibold leading-tight truncate">
                {r.sector.name.replace(/^Nifty /, '')}
              </div>
              <div className="text-lg font-bold tabular-nums">{fmtPct(r.avgPct)}</div>
              <div className="text-[11px] opacity-90 tabular-nums">
                {r.indexPct != null ? `Index ${fmtPct(r.indexPct)} | ` : ''}
                {r.advancers} up / {r.decliners} down
              </div>
            </button>
          )
        })}
      </div>

      {/* Stock grids */}
      <div className="grid gap-4 lg:grid-cols-2">
        {visibleSectors.map((r) => {
          const stocks = [...r.stocks].sort((a, b) => compareByChange(a.quote?.pct, b.quote?.pct))
          return (
            <Card key={r.sector.key}>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-baseline justify-between gap-2 text-base">
                  <span>{r.sector.name}</span>
                  <span className="text-sm font-normal text-muted-foreground tabular-nums">
                    avg {fmtPct(r.avgPct)}
                    {r.indexPct != null ? ` | index ${fmtPct(r.indexPct)}` : ''} | {r.advancers} up
                    / {r.decliners} down
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid gap-1 grid-cols-3 sm:grid-cols-4 md:grid-cols-5">
                  {stocks.map((s) => {
                    const match = needle !== '' && s.symbol.includes(needle)
                    const dim = needle !== '' && !match
                    return (
                      <Tooltip key={s.symbol}>
                        <TooltipTrigger asChild>
                          <div
                            className={`rounded border px-1.5 py-1 text-center cursor-default ${
                              match ? 'ring-2 ring-primary' : ''
                            } ${dim ? 'opacity-30' : ''}`}
                            style={tileStyle(s.quote?.pct)}
                          >
                            <div className="text-[11px] font-semibold truncate">{s.symbol}</div>
                            <div className="text-xs tabular-nums">{fmtPct(s.quote?.pct)}</div>
                          </div>
                        </TooltipTrigger>
                        <TooltipContent>
                          <div className="text-xs space-y-0.5">
                            <div className="font-semibold">{s.name || s.symbol}</div>
                            <div>{s.industry}</div>
                            <div className="tabular-nums">
                              LTP {fmtPrice(s.quote?.ltp)} | prev close{' '}
                              {fmtPrice(s.quote?.prevClose)}
                            </div>
                            <div className="tabular-nums">Change {fmtPct(s.quote?.pct)}</div>
                          </div>
                        </TooltipContent>
                      </Tooltip>
                    )
                  })}
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      {!loading && sectors.length > 0 && pricedTotal === 0 && (
        <p className="text-sm text-muted-foreground">
          Waiting for prices. If this persists, check that the broker is logged in and the WebSocket
          server is running.
        </p>
      )}
    </div>
  )
}

function Legend() {
  const steps = [-3, -2, -1, -0.25, 0.25, 1, 2, 3]
  return (
    <div className="flex items-center gap-1 text-[11px] text-muted-foreground">
      <span>Change:</span>
      {steps.map((p) => (
        <span key={p} className="rounded px-1.5 py-0.5 tabular-nums" style={tileStyle(p)}>
          {p > 0 ? '+' : ''}
          {p}%
        </span>
      ))}
      <span className="ml-1">(colour saturates at {COLOR_CAP_PCT}%)</span>
    </div>
  )
}
