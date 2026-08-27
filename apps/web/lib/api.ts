const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"

export async function fetchAPI<T>(endpoint: string): Promise<T> {
  const res = await fetch(`${API_URL}/api/v1${endpoint}`, {
    next: { revalidate: 10 },
  })
  if (!res.ok) throw new Error(`API error: ${res.status}`)
  return res.json()
}

export async function postAPI<T>(endpoint: string, body?: any): Promise<T> {
  const res = await fetch(`${API_URL}/api/v1${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) throw new Error(`API error: ${res.status}`)
  return res.json()
}
