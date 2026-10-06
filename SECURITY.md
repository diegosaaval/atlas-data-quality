# Seguridad

ATLAS es un proyecto de portafolio con datos 100% sintéticos: no almacena información real de personas ni credenciales.

## Cómo reportar un problema
Si encuentras una vulnerabilidad, por favor **no abras un issue público**. Usa **[Report a vulnerability](https://github.com/diegosaaval/atlas-data-quality/security/advisories/new)** (pestaña *Security* del repositorio). Respondo en un máximo de 7 días.

## Medidas implementadas
| Riesgo | Medida |
|---|---|
| Reglas SQL escritas por usuarios | Solo lectura a nivel de motor (`PRAGMA query_only`), sin `;`, comentarios ni comandos de escritura, máximo 500 caracteres, **tiempo límite de 0,5 s** y tope de filas. SQLite sin carga de extensiones. |
| Abuso de la demo pública | Límite de 40 acciones por minuto por visitante, máximo 15 reglas de visitantes, reglas base protegidas, sin reinicio ni pausa permanente, tope de conexiones en vivo. |
| XSS / clickjacking | Todo el contenido dinámico se escapa en la interfaz; encabezados `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy` y `Permissions-Policy`. |
| Secretos | Ninguno en el repositorio. La API key opcional del copiloto (`ANTHROPIC_API_KEY`) se lee de variables de entorno. |
| Contenedor | Imagen *slim*, usuario sin privilegios (`uid 10001`), sistema de archivos de solo lectura en `docker-compose`. |
| Dependencias | Alertas de seguridad de GitHub, `pip-audit` y CodeQL en cada cambio. Las actualizaciones se aplican y se prueban con el CI antes de integrarlas. |

## Fuera de alcance
La demo no tiene autenticación: es pública a propósito y se reinicia sola. En un uso real, ATLAS iría detrás del inicio de sesión corporativo (SSO) y se conectaría a las bases con un usuario de **solo lectura**.
