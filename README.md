# BAIC / Chery Price Scraper - Mercado Libre Argentina

Scraper automatizado que obtiene precios de autos BAIC y Chery desde Mercado Libre Argentina, publica un dashboard en GitHub Pages y envia un reporte por email.

## Como funciona

1. Obtiene publicaciones de Mercado Libre usando los proveedores configurados.
2. Extrae titulo, vendedor, precio, ubicacion y link de cada publicacion.
3. Agrupa los resultados por marca/modelo y calcula promedios sin mezclar pesos y dolares.
4. Compara publicaciones propias contra competencia de concesionarias por modelo.
5. Actualiza el dashboard y envia el reporte por email via Gmail.

## Ejecucion local

```bash
# Instalar dependencias
pip install -r requirements.txt

# Ejecutar sin email configurado imprime el resultado en consola
python main.py

# Ejecutar con email
set EMAIL_SENDER=tu-email@gmail.com
set EMAIL_PASSWORD=tu-app-password-aqui
set EMAIL_RECIPIENTS=destinatario@email.com
python main.py
```

## Tests

```bash
python -m unittest discover -s tests -v
```

La suite cubre calculo de moneda dominante, deteccion de sellers propios, benchmark contra competencia y contenido del email.

## Configurar sellers propios

El email compara "Nosotros" contra competencia usando los sellers configurados como propios.

| Variable | Uso |
|---|---|
| `OWN_SELLER_KEYWORDS` | Lista global separada por comas |
| `OWN_SELLER_KEYWORDS_BAIC` | Sellers propios solo para BAIC |
| `OWN_SELLER_KEYWORDS_CHERY` | Sellers propios solo para Chery |

Default actual para BAIC: `baic san jorge,baic by one fan,nationbaic`. Chery queda vacio hasta configurar sellers propios.

## Configurar Gmail App Password

1. Activar la verificacion en 2 pasos en la cuenta de Gmail.
2. Ir a: Cuenta de Google > Seguridad > Contrasenas de aplicaciones.
3. Crear una contrasena de aplicacion para "Correo" / "Otro (nombre personalizado)".
4. Copiar la contrasena de 16 caracteres. Ese es el valor para `EMAIL_PASSWORD`.

## Configurar GitHub Actions

### Secrets necesarios

En el repositorio de GitHub, ir a **Settings > Secrets and variables > Actions** y agregar:

| Secret | Valor |
|---|---|
| `EMAIL_SENDER` | Tu email de Gmail |
| `EMAIL_PASSWORD` | La contrasena de aplicacion de 16 caracteres |
| `EMAIL_RECIPIENTS` | `email1@ejemplo.com,email2@ejemplo.com` |

### Variables recomendadas

En **Settings > Secrets and variables > Actions > Variables** agregar los sellers propios:

| Variable | Ejemplo |
|---|---|
| `OWN_SELLER_KEYWORDS_BAIC` | `baic san jorge,baic by one fan,nationbaic` |
| `OWN_SELLER_KEYWORDS_CHERY` | `nombre concesionaria,nombre grupo` |

### Horario

El scraper se ejecuta automaticamente cada 4 dias a las 12:00 UTC (9:00 AM Argentina).

Tambien se puede ejecutar manualmente desde la pestana **Actions > BAIC Price Scraper > Run workflow**.
