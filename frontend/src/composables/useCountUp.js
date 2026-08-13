/**
 * 数字滚动动画：从 0 缓动到目标值，配合 tabular-nums 不跳动。
 * 尊重 prefers-reduced-motion：直接显示终值。
 */
import { ref, watch, onMounted } from 'vue'

export function useCountUp(target, duration = 600) {
  const display = ref(0)

  function animate(to) {
    if (typeof to !== 'number' || Number.isNaN(to)) {
      display.value = to
      return
    }
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced || duration <= 0) {
      display.value = to
      return
    }
    const start = performance.now()
    const from = 0
    const tick = (now) => {
      const t = Math.min((now - start) / duration, 1)
      // easeOutQuart
      const eased = 1 - Math.pow(1 - t, 4)
      display.value = from + (to - from) * eased
      if (t < 1) requestAnimationFrame(tick)
      else display.value = to
    }
    requestAnimationFrame(tick)
  }

  onMounted(() => animate(target.value))
  watch(target, (v) => animate(v))

  return display
}
