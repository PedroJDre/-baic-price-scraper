# BAIC Price Scraper - Mercado Libre Argentina

Scraper automatizado que obtiene precios de autos BAIC desde Mercado Libre Argentina y envia los resultados por email.

## Como funciona

1. Scrapes HTML de `autos.mercadolibre.com.ar/baic/` (todas las paginas)
2. Extrae titulo, vendedor, precio, ubicacion y link de cada publicacion
3. Agrupa los resultados por modelo y ordena por precio
4. Envia el reporte por email via Gmail

## Ejecucion local

```bash
# Instalar dependencias
pip install -r requirements.txt

# Ejecutar sin email (imprime en consola)
python main.py

# Ejecutar con email
set EMAIL_SENDER=tu-email@gmail.com
set EMAIL_PASSWORD=tu-app-password-aqui
set EMAIL_RECIPIENTS=destinatario@email.com
python main.py
```

## Configurar Gmail App Password

1. Activar la verificacion en 2 pasos en la cuenta de Gmail
2. Ir a: Cuenta de Google > Seguridad > Contrasenas de aplicaciones
3. Crear una contrasena de aplicacion para "Correo" / "Otro (nombre personalizado)"
4. Copiar la contrasena de 16 caracteres (es el valor para `EMAIL_PASSWORD`)

## Configurar GitHub Actions

### Secrets necesarios

En el repositorio de GitHub, ir a **Settings > Secrets and variables > Actions** y agregar:

| Secret | Valor |
|---|---|
| `EMAIL_SENDER` | Tu email de Gmail |
| `EMAIL_PASSWORD` | La contrasena de aplicacion de 16 caracteres |
| `EMAIL_RECIPIENTS` | `email1@ejemplo.com,email2@ejemplo.com` |

### Horario

El scraper se ejecuta automaticamente los **Lunes, Miercoles y Viernes a las 10:00 AM (hora Argentina)**.

Tambien se puede ejecutar manualmente desde la pestana **Actions > BAIC Price Scraper > Run workflow**.
