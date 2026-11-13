/** Explicit living-room launches. Speaker location does not change the target. */
export interface TvAppIntent {
  app: 'netflix' | 'youtube'
  scriptEntity: string
  packageId: string
  label: string
}

export interface FireplaceIntent {
  scriptEntity: 'script.living_room_fireplace'
  label: 'камин'
  marker: 'fireplace-fastily-1080p.webm'
}

interface NativePlayer {
  state: string
  attributes?: { app_id?: unknown, media_title?: unknown }
}

interface AppActions {
  callScript: (entityId: string) => Promise<void>
  readNative: () => Promise<NativePlayer | undefined>
  sleep?: (milliseconds: number) => Promise<void>
}

interface FireplaceReadiness {
  state: string
  attributes?: { owned?: unknown, file_matches?: unknown, repeat?: unknown, observed_at?: unknown }
}

interface FireplaceActions extends AppActions {
  readReadiness: () => Promise<FireplaceReadiness | undefined>
}

const APPS: Record<TvAppIntent['app'], TvAppIntent> = {
  netflix: {
    app: 'netflix', scriptEntity: 'script.living_room_netflix',
    packageId: 'com.netflix.ninja', label: 'Netflix'
  },
  youtube: {
    app: 'youtube', scriptEntity: 'script.living_room_youtube',
    packageId: 'com.google.android.youtube.tv', label: 'YouTube'
  }
}
const ACTIVE_STATES = new Set(['on', 'idle', 'playing', 'paused', 'buffering'])
const FIREPLACE: FireplaceIntent = {
  scriptEntity: 'script.living_room_fireplace', label: 'камин', marker: 'fireplace-fastily-1080p.webm'
}
const FIREPLACE_TITLES = new Set([
  FIREPLACE.marker, 'fireplace-fastily-1080p',
  '/home/ultra/KodiLibrary/Ambience/' + FIREPLACE.marker
])

function imperativeWords (input: unknown): string | null {
  if (typeof input !== 'string' || input.length > 240) return null
  const text = input.normalize('NFKC').toLowerCase().replace(/ё/g, 'е')
  if (text.includes('?')) return null
  return text.trim().replace(/[.!]+$/, '').replace(/,/g, ' ').replace(/\s+/g, ' ').trim()
}

/** Match a complete imperative, never a substring inside a question or music request. */
export function parseTvAppIntent (input: unknown): TvAppIntent | null {
  const words = imperativeWords(input)
  if (words === null) return null
  const match = /^(?:алиса )?(?:пожалуйста )?(?:открой|включи|запусти) (?:пожалуйста )?(?:на (?:телевизоре|тв|телеке|телике) )?(netflix|нетфликс|youtube|ютуб|ютюб|ютьюб)(?: на (?:телевизоре|тв|телеке|телике))?(?: пожалуйста)?$/.exec(words)
  if (match === null) return null
  return { ...APPS[/^(netflix|нетфликс)$/.test(match[1]) ? 'netflix' : 'youtube'] }
}

/** This is a fixed video on the projector, never a heater or caller-provided URL. */
export function parseFireplaceIntent (input: unknown): FireplaceIntent | null {
  const words = imperativeWords(input)
  if (words === null) return null
  const match = /^(?:алиса )?(?:пожалуйста )?(?:открой|включи|запусти) (?:пожалуйста )?(?:на проекторе )?камин(?: на проекторе)?(?: пожалуйста)?$/.test(words)
  return match ? { ...FIREPLACE } : null
}

/** One accepted launch, followed by bounded observation. Uncertainty never causes a retry. */
async function executeObserved (
  scriptEntity: string, actions: AppActions, observed: (native: NativePlayer) => boolean
): Promise<{ status: 'confirmed' | 'pending' }> {
  await actions.callScript(scriptEntity)
  const sleep = actions.sleep ?? (async milliseconds => await new Promise(resolve => setTimeout(resolve, milliseconds)))
  const deadline = Date.now() + 4000
  for (let attempt = 0; attempt < 12 && Date.now() < deadline; attempt++) {
    let native: NativePlayer | undefined
    try {
      native = await actions.readNative()
    } catch {
      // The service was accepted; a failed observation does not prove a failed launch.
      return { status: 'pending' }
    }
    if (native !== undefined && observed(native)) return { status: 'confirmed' }
    if (attempt < 11 && Date.now() < deadline) await sleep(250)
  }
  return { status: 'pending' }
}

export async function executeTvAppIntent (
  intent: TvAppIntent, actions: AppActions
): Promise<{ status: 'confirmed' | 'pending' }> {
  return await executeObserved(intent.scriptEntity, actions, native =>
    ACTIVE_STATES.has(native.state) && native.attributes?.app_id === intent.packageId)
}

function fireplaceReady (proof: FireplaceReadiness | undefined, launchStarted: number): boolean {
  const attrs = proof?.attributes
  if (proof?.state !== 'playing' || attrs?.owned !== true || attrs.file_matches !== true) return false
  if (attrs.repeat !== 'one' || typeof attrs.observed_at !== 'string') return false
  const observedAt = Date.parse(attrs.observed_at)
  return Number.isFinite(observedAt) && observedAt >= launchStarted && observedAt <= Date.now() + 2000
}

export async function executeFireplaceIntent (
  _intent: FireplaceIntent, actions: FireplaceActions
): Promise<{ status: 'confirmed' | 'pending' }> {
  const launchStarted = Date.now()
  return await executeObserved(FIREPLACE.scriptEntity, {
    ...actions,
    readNative: async () => {
      const [native, proof] = await Promise.all([actions.readNative(), actions.readReadiness()])
      return fireplaceReady(proof, launchStarted) ? native : undefined
    }
  }, native => {
    const title = native.attributes?.media_title
    return native.state === 'playing' && typeof title === 'string' && FIREPLACE_TITLES.has(title)
  })
}
