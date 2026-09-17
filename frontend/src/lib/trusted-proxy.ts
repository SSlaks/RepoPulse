export const TRUSTED_CLIENT_IP_HEADER = "X-RepoPulse-Client-IP";
export const TRUSTED_PROXY_TOKEN_HEADER = "X-RepoPulse-Proxy-Token";
export const INTERNAL_SERVICE_TOKEN_HEADER = "X-Internal-Service-Token";

export function copyTrustedProxyHeaders(source: Pick<Headers, "get">): Headers {
  const headers = new Headers();
  for (const name of [TRUSTED_CLIENT_IP_HEADER, TRUSTED_PROXY_TOKEN_HEADER]) {
    const value = source.get(name);
    if (value) headers.set(name, value);
  }
  return headers;
}
