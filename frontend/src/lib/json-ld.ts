const JSON_LD_ESCAPE_MAP: Record<string, string> = {
  "<": "\\u003c",
  ">": "\\u003e",
  "&": "\\u0026",
  "\u2028": "\\u2028",
  "\u2029": "\\u2029",
};

export function serializeJsonLd(value: unknown): string {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) {
    throw new TypeError("JSON-LD value is not serializable");
  }

  return serialized.replace(/[<>&\u2028\u2029]/g, (character) => JSON_LD_ESCAPE_MAP[character]);
}
