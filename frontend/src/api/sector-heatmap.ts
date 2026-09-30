import { webClient } from './client'

export interface SectorStock {
  symbol: string
  name: string
  industry: string
}

export interface Sector {
  key: string
  name: string
  /** OpenAlgo NSE_INDEX symbol of the official index, when the broker lists it */
  index_symbol: string | null
  stocks: SectorStock[]
}

export interface SectorSymbol {
  symbol: string
  exchange: string
}

export interface SectorUniverseData {
  sectors: Sector[]
  symbols: SectorSymbol[]
  counts: {
    sectors: number
    stocks: number
    missing: number
  }
  /** When the constituent lists were last refreshed from NSE */
  generated_at: string | null
}

export interface SectorUniverseResponse {
  status: 'success' | 'error'
  message?: string
  data?: SectorUniverseData
}

export const sectorHeatmapApi = {
  /**
   * Fetch every NSE sectoral index with its constituents. Session-authenticated;
   * live prices come from the shared market-data WebSocket, not this call.
   */
  getUniverse: async (): Promise<SectorUniverseResponse> => {
    const response = await webClient.get<SectorUniverseResponse>('/sectorheatmap/api/universe')
    return response.data
  },
}
