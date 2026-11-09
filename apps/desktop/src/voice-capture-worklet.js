// Ephemeral mono PCM16 at 24 kHz. The worklet produces silence on its output; microphone audio is never monitored
// through the speakers or written to storage. Resampling phase is preserved across input render blocks.
class JanaVoiceCapture extends globalThis.AudioWorkletProcessor {
  constructor() {
    super();
    this.previous = 0;
    this.havePrevious = false;
    this.phase = 0;
    this.output = new Int16Array(480); // 20 ms: bound capture latency and the partial frame at key-up
    this.used = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (!channels?.length) return true;
    const length = channels[0].length;
    for (let index = 0; index < length; index++) {
      let sample = 0;
      for (const channel of channels) sample += channel[index] / channels.length;
      if (!this.havePrevious) { this.previous = sample; this.havePrevious = true; continue; }
      while (this.phase < 1) {
        const value = Math.max(-1, Math.min(1, this.previous + (sample - this.previous) * this.phase));
        this.output[this.used++] = Math.round(value * (value < 0 ? 32768 : 32767));
        if (this.used === this.output.length) {
          this.port.postMessage(this.output.buffer, [this.output.buffer]);
          this.output = new Int16Array(480);
          this.used = 0;
        }
        this.phase += globalThis.sampleRate / 24000;
      }
      this.phase -= 1;
      this.previous = sample;
    }
    return true;
  }
}
globalThis.registerProcessor("jana-voice-capture", JanaVoiceCapture);
