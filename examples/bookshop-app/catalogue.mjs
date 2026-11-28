// The shop's catalogue: the page lists it and the MCP tool searches it. A real shop reads its own database here.

import { readFileSync } from 'node:fs'

export const books = JSON.parse(readFileSync(new URL('./catalogue.json', import.meta.url), 'utf8'))

/** Books whose title, author or genre contains every word of the query (all books for an empty query). */
export function search(query = '', { inStockOnly = false } = {}) {
  const words = String(query).toLowerCase().split(/\s+/).filter(Boolean)
  return books.filter((b) => {
    if (inStockOnly && b.stock === 0) return false
    const text = `${b.title} ${b.author} ${b.genre}`.toLowerCase()
    return words.every((w) => text.includes(w))
  })
}
