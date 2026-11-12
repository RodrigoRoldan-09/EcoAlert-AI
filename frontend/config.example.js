// Ejemplo de configuración del frontend.
//
// Al desplegar, el stack de CloudFormation escribe automáticamente el archivo
// real "config.js" en el bucket del sitio con la URL de tu API, así que NO
// necesitas subirlo. Solo para pruebas locales: copia este archivo como
// "config.js" (está en .gitignore) y reemplaza la URL por el valor del output
// "ApiUrl" del stack.
window.ECOALERT_API_URL = "https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/report";
