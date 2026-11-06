// The reference price source's name ([pricing.reference] name in kiln.toml), sent in the
// snapshot as `pricing.reference`. Labels use it so a new reference model needs no UI change.
const FALLBACK = 'Reference';

export function referenceName(pricing) {
  return (pricing && pricing.reference) || FALLBACK;
}

// "Claude Sonnet 5.5" -> "Sonnet 5.5" for tight labels.
export function referenceShort(pricing) {
  return referenceName(pricing).replace(/^Claude\s+/, '');
}

// hasReference(pricing) -> whether the snapshot carries reference prices. Without [pricing.reference] in
// kiln.toml the server sends `pricing: null`, and the API-cost figures are hidden instead of reading $0.
export function hasReference(pricing) {
  return Boolean(pricing && pricing.reference);
}
