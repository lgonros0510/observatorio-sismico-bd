# Validación del material base

Comprobaciones realizadas el 27 de octubre de 2026:

- Instalación de las dependencias fijadas y generación de `package-lock.json`.
- `npm run build`: compilación de producción correcta, con TypeScript estricto y comprobación de plantillas Angular.
- Aviso de compilación: Leaflet 1.9.4 utiliza CommonJS; no impide generar el build.
- Catálogo JSON válido: 177 entidades, 177 códigos `ADM0_A3` distintos, geometrías Polygon/MultiPolygon.
- El build incluye config.json, el catálogo, la muestra y `_redirects`.
- Las muestras están etiquetadas como sintéticas, separadas del modo API.

Límites de la validación del material base:

- No se completó una prueba visual en navegador: el entorno no disponía de Chromium y no pudo descargar su ejecutable correctamente.
- No se ha desplegado una cuenta real de Netlify ni abierto un túnel ngrok del alumnado.

## Validación de la implementación de referencia

Comprobaciones realizadas el 29 de octubre de 2026:

- `npm ci` y `npm run build`: correctos; permanece el aviso conocido de Leaflet CommonJS.
- Python 3.9.13: `compileall`, importación de FastAPI/OpenAPI y ocho pruebas unitarias de transformaciones, geografía, caché histórica y exclusión de casos replay correctas.
- `docker compose --env-file .env.example config --quiet`: correcto.
- Medición sintética de 1.000, 5.000 y 10.000 registros; método y resultados en `MEMORIA.md`.

No se ejecutó `docker compose up`: el cliente Docker está instalado, pero no había motor Docker disponible en el equipo durante esta validación. Por tanto, productor–Kafka–consumidor–PostgreSQL, carga real del histórico y peticiones CORS quedan pendientes de la prueba de integración local; no se afirman como resultados verificados.

Antes de defender, ejecutar:

1. `npm ci` y `npm start` en frontend.
2. `Copy-Item .env.example .env`, completar configuración y arrancar Compose siguiendo `README.md`.
3. Cargar el histórico 2025, verificar salud, ingesta y consulta de eventos.
4. Probar los casos de aceptación de duplicados, revisiones, nulos, cuarentena, costa, reinicio y replay.
5. En modo demo, verificar cuatro eventos ficticios y fronteras; abrir una ficha y el selector de capas.
6. Comprobar zoom, leyenda y atribución del mapa base.
7. Configurar ngrok, confirmar la petición OPTIONS/GET y datos reales desde el frontend.
8. Cerrar y reabrir el túnel; comprobar aviso y recuperación en el siguiente refresco.
9. Publicar en Netlify y repetir desde otro dispositivo.
