/**
 * The backend serializes naive-UTC datetimes (Python's datetime.utcnow(),
 * no tzinfo) as ISO strings with no "Z"/offset suffix, e.g.
 * "2026-08-26T14:50:33.687615". JavaScript's `Date` constructor treats a
 * date-time string with no timezone designator as LOCAL time (a
 * well-known ECMAScript gotcha), which silently shifts every such
 * timestamp by the browser's UTC offset -- found via manual browser
 * verification when a sync that had just happened showed as "Updated 5h
 * ago". Every backend timestamp must be parsed through this helper, never
 * passed to `new Date(...)` directly.
 */
export function parseUtcDate(isoString: string): Date {
  const hasTimezone = /Z$|[+-]\d{2}:\d{2}$/.test(isoString)
  return new Date(hasTimezone ? isoString : `${isoString}Z`)
}
