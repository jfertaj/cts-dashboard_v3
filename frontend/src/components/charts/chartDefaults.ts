/**
 * Recharts: la animación de entrada NO es decoración, es la que hace EXISTIR la
 * marca. `<Bar>` interpola su alto de 0 a su valor y `<Rectangle>` devuelve
 * `null` cuando el alto es 0, así que a t=0 el `<g class="recharts-bar-rectangle">`
 * queda HUECO — sin `<path>` dentro. La línea hace lo propio con
 * `stroke-dasharray: 0px, <largo>`: cero píxeles dibujados.
 *
 * Quien mueve esa interpolación es react-smooth, con `requestAnimationFrame`
 * (`react-smooth/setRafTimeout.js`). Y rAF no entrega frames en un tab de fondo,
 * ocluido o con el hilo principal saturado: la animación se queda clavada en su
 * primer paso y el usuario ve el panel VACÍO — con ejes, rejilla y leyenda, que
 * no animan, pero sin datos. Ése fue el bug de las pestañas en blanco: el modal
 * viejo montaba un único gráfico y animaba una vez, mientras que el contenedor
 * con pestañas re-monta un gráfico en CADA cambio de pestaña y reabre esa
 * ventana una y otra vez.
 *
 * Con la animación apagada la marca se pinta directamente desde el dato, en el
 * primer render, sin depender de que llegue ningún frame. Pineado por
 * `tests/e2e/charts-animation.spec.ts`, que mata rAF y exige geometría real.
 */
export const NO_ENTRY_ANIMATION = { isAnimationActive: false } as const;
