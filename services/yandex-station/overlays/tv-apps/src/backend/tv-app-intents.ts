/** Explicit TV application requests. Speaker location does not change the target. */
export interface TvAppIntent {
  app: 'netflix' | 'youtube'
  scriptEntity: string
  packageId: string
  label: string
}

interface NativePlayer {
  state: string
  attributes?: { app_id?: unknown }
}

interface AppActions {
  callScript: (entityId: string) => Promise<void>
  readNative: () => Promise<NativePlayer | undefined>
  sleep?: (milliseconds: number) => Promise<void>
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

/** Match a complete imperative, never a substring inside a question or music request. */
export function parseTvAppIntent (input: unknown): TvAppIntent | null {
  if (typeof input !== 'string' || input.length > 240) return null
  const text = input.normalize('NFKC').toLowerCase().replace(/ё/g, 'е')
  if (text.includes('?')) return null
  const words = text.trim().replace(/[.!]+$/, '').replace(/,/g, ' ').replace(/\s+/g, ' ').trim()
  const match = /^(?:алиса )?(?:пожалуйста )?(?:открой|включи|запусти) (?:пожалуйста )?(?:на (?:телевизоре|тв|телеке|телике) )?(netflix|нетфликс|youtube|ютуб|ютюб|ютьюб)(?: на (?:телевизоре|тв|телеке|телике))?(?: пожалуйста)?$/.exec(words)
  if (match === null) return null
  return { ...APPS[/^(netflix|нетфликс)$/.test(match[1]) ? 'netflix' : 'youtube'] }
}

/** One accepted launch, followed by bounded observation. Uncertainty never causes a retry. */
export async function executeTvAppIntent (
  intent: TvAppIntent, actions: AppActions
): Promise<{ status: 'confirmed' | 'pending' }> {
  await actions.callScript(intent.scriptEntity)
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
    if (native !== undefined && ACTIVE_STATES.has(native.state) &&
        native.attributes?.app_id === intent.packageId) return { status: 'confirmed' }
    if (attempt < 11 && Date.now() < deadline) await sleep(250)
  }
  return { status: 'pending' }
}
