import { api, type Schemas } from './api'

export type Features = Schemas['FeaturesOut']

let featuresOnce: Promise<Features> | null = null

/** 服务端配置决定的可选能力与默认值（GET /api/features）：整个页面只取一次，失败时下次重新取。 */
export function loadFeatures(): Promise<Features> {
  featuresOnce ??= api.features().catch((e: unknown) => {
    featuresOnce = null
    throw e
  })
  return featuresOnce
}
