// Client-minted ids (API-07): `cli_` plus a 26-character Crockford base32 ULID, monotonic within a millisecond.
const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

let lastTime = -1;
let lastRandom: number[] = [];

function randomDigits(): number[] {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b % 32);
}

export function ulid(now: number = Date.now()): string {
  let random: number[];
  if (now === lastTime) {
    random = [...lastRandom];
    for (let i = random.length - 1; i >= 0; i -= 1) {
      const digit = (random[i] ?? 0) + 1;
      if (digit < 32) {
        random[i] = digit;
        break;
      }
      random[i] = 0;
    }
  } else {
    random = randomDigits();
  }
  lastTime = now;
  lastRandom = random;
  let time = "";
  let t = now;
  for (let i = 0; i < 10; i += 1) {
    time = (ALPHABET[t % 32] ?? "0") + time;
    t = Math.floor(t / 32);
  }
  return time + random.map((d) => ALPHABET[d] ?? "0").join("");
}

export function clientId(): string {
  return `cli_${ulid()}`;
}
