/* EcoAlert AI - lógica del frontend (sin frameworks).
 * Todos los valores dinámicos se insertan con textContent para evitar XSS. */
(function () {
  "use strict";

  var API_URL = window.ECOALERT_API_URL;
  var MAX_CHARS = 2000;
  var TIMEOUT_MS = 35000;
  var NIVELES = ["BAJA", "MEDIA", "ALTA"];

  var $ = function (id) { return document.getElementById(id); };
  var form = $("form-reporte");
  var textarea = $("texto");
  var contador = $("contador");
  var errorForm = $("error-form");
  var boton = $("btn-enviar");
  var botonTexto = $("btn-texto");
  var resultados = $("resultados");

  function el(tag, clase, texto) {
    var nodo = document.createElement(tag);
    if (clase) { nodo.className = clase; }
    if (texto !== undefined && texto !== null) { nodo.textContent = String(texto); }
    return nodo;
  }

  function limpiar(nodo) {
    while (nodo.firstChild) { nodo.removeChild(nodo.firstChild); }
  }

  function badge(nivel, grande) {
    var valido = NIVELES.indexOf(nivel) !== -1;
    var clase = "badge" + (valido ? " badge--" + nivel : "") + (grande ? " badge--grande" : "");
    return el("span", clase, valido ? nivel : "N/D");
  }

  function segundos(valor) {
    return typeof valor === "number" ? valor.toFixed(2) + " s" : "N/D";
  }

  function formatearFecha(iso) {
    var fecha = new Date(iso);
    if (isNaN(fecha.getTime())) { return iso || "N/D"; }
    return fecha.toLocaleString("es-MX", { dateStyle: "medium", timeStyle: "short" });
  }

  function actualizarContador() {
    contador.textContent = textarea.value.length + " / " + MAX_CHARS;
  }

  function mostrarError(mensaje) {
    errorForm.textContent = mensaje;
    errorForm.hidden = false;
    textarea.setAttribute("aria-invalid", "true");
  }

  function ocultarError() {
    errorForm.hidden = true;
    errorForm.textContent = "";
    textarea.removeAttribute("aria-invalid");
  }

  function setCargando(cargando) {
    boton.disabled = cargando;
    boton.classList.toggle("cargando", cargando);
    botonTexto.textContent = cargando ? "Analizando…" : "Analizar reporte";
    resultados.setAttribute("aria-busy", cargando ? "true" : "false");
  }

  function fila(lista, etiqueta, valor) {
    var div = el("div");
    div.appendChild(el("dt", null, etiqueta));
    var dd = el("dd");
    if (valor instanceof Node) { dd.appendChild(valor); } else { dd.textContent = valor; }
    div.appendChild(dd);
    lista.appendChild(div);
  }

  function renderModelo(contenedor, nivel, filas, error) {
    limpiar(contenedor);
    contenedor.appendChild(badge(nivel, true));
    if (!nivel) {
      contenedor.appendChild(el("p", "modelo-error", "No disponible" + (error ? ": " + error : ".")));
    }
    var dl = el("dl", "datos");
    filas.forEach(function (f) { fila(dl, f[0], f[1]); });
    contenedor.appendChild(dl);
  }

  function barras(seg) {
    var cont = $("barras");
    limpiar(cont);
    var datos = [
      ["Comprehend", seg && seg.comprehend, ""],
      ["Bedrock", seg && seg.bedrock, " barra-relleno--bedrock"]
    ];
    var maximo = Math.max(0.01, (seg && seg.comprehend) || 0, (seg && seg.bedrock) || 0);
    datos.forEach(function (d) {
      var valor = typeof d[1] === "number" ? d[1] : 0;
      var filaBarra = el("div", "barra-fila");
      filaBarra.setAttribute("role", "img");
      filaBarra.setAttribute("aria-label", d[0] + ": " + segundos(d[1]));
      var nombre = el("span", null, d[0]);
      nombre.setAttribute("aria-hidden", "true");
      var pista = el("div", "barra-pista");
      pista.setAttribute("aria-hidden", "true");
      var relleno = el("div", "barra-relleno" + d[2]);
      relleno.style.width = Math.round((valor / maximo) * 100) + "%";
      pista.appendChild(relleno);
      var texto = el("span", "barra-valor", segundos(d[1]));
      texto.setAttribute("aria-hidden", "true");
      filaBarra.appendChild(nombre);
      filaBarra.appendChild(pista);
      filaBarra.appendChild(texto);
      cont.appendChild(filaBarra);
    });
  }

  function renderAviso(data) {
    var aviso = $("aviso-alerta");
    var errores = data.errores || {};
    aviso.className = "aviso";
    if (data.alertaEnviada) {
      var n = data.destinatarios || 0;
      aviso.textContent = "Se envió una alerta por correo a " + n + " usuario(s) registrado(s).";
      aviso.classList.add("aviso--ok");
      aviso.hidden = false;
    } else if (data.gravedadFinal === "ALTA") {
      aviso.textContent = "Gravedad ALTA, pero no se envió la alerta por correo" +
        (errores.ses ? ": " + errores.ses : ".");
      aviso.classList.add("aviso--neutro");
      aviso.hidden = false;
    } else {
      aviso.hidden = true;
      aviso.textContent = "";
    }
  }

  function render(data) {
    var errores = data.errores || {};
    var tokens = data.tokens || {};
    var seg = data.segundos || {};

    renderAviso(data);

    var final = $("gravedad-final");
    limpiar(final);
    final.appendChild(badge(data.gravedadFinal, true));

    $("res-ubicacion").textContent = data.ubicacion || "No identificada";
    $("res-ticket").textContent = data.ticketId || "N/D";
    $("res-fecha").textContent = formatearFecha(data.fecha);

    var sentimiento = data.sentimiento
      ? data.sentimiento + (typeof data.puntajeNegativo === "number"
        ? " (negativo " + data.puntajeNegativo.toFixed(2) + ")" : "")
      : "N/D";
    renderModelo($("card-comprehend"), data.comprehend, [
      ["Sentimiento", sentimiento],
      ["Tiempo", segundos(seg.comprehend)],
      ["Consumo", (data.unidadesComprehend || 0) + " unidad(es) de texto (100 caracteres)"]
    ], errores.comprehend);

    renderModelo($("card-bedrock"), data.bedrock, [
      ["Justificación", data.justificacion || "N/D"],
      ["Tiempo", segundos(seg.bedrock)],
      ["Tokens", (tokens.entrada || 0) + " entrada · " + (tokens.salida || 0) +
        " salida · " + (tokens.total || 0) + " total"],
      ["Modelo", el("span", "mono", data.modeloBedrock || "N/D")]
    ], errores.bedrock);

    barras(seg);
    $("res-json").textContent = JSON.stringify(data, null, 2);

    resultados.hidden = false;
    $("titulo-resultados").focus();
  }

  function enviar(evento) {
    evento.preventDefault();
    ocultarError();
    var texto = textarea.value.trim();
    if (!texto) {
      mostrarError("Escribe una descripción del incidente.");
      textarea.focus();
      return;
    }
    if (texto.length > MAX_CHARS) {
      mostrarError("El reporte excede el máximo de " + MAX_CHARS + " caracteres.");
      textarea.focus();
      return;
    }

    setCargando(true);
    var controlador = new AbortController();
    var temporizador = setTimeout(function () { controlador.abort(); }, TIMEOUT_MS);

    fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texto: texto }),
      signal: controlador.signal
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) {
            throw new Error(data.error || "Error del servidor (código " + res.status + ").");
          }
          return data;
        });
      })
      .then(render)
      .catch(function (err) {
        if (err.name === "AbortError") {
          mostrarError("La API tardó demasiado en responder. Intenta de nuevo.");
        } else if (err instanceof TypeError) {
          mostrarError("No se pudo conectar con la API.");
        } else {
          mostrarError(err.message);
        }
      })
      .then(function () {
        clearTimeout(temporizador);
        setCargando(false);
      });
  }

  textarea.addEventListener("input", actualizarContador);

  Array.prototype.forEach.call(document.querySelectorAll(".ejemplo"), function (b) {
    b.addEventListener("click", function () {
      textarea.value = b.getAttribute("data-texto") || "";
      actualizarContador();
      ocultarError();
      textarea.focus();
    });
  });

  form.addEventListener("submit", enviar);
  actualizarContador();

  if (!API_URL) {
    mostrarError("Falta config.js con la URL de la API. Despliega el stack o copia config.example.js.");
    boton.disabled = true;
  }
})();
