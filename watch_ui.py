"""The look of the watch windows, and the chat window both places open.

server.py's single-run viewer (Agent Intern) and swarm_watch's per-worker detail
window show the same thing: the prompt as a bubble, the agent's live steps as a
timeline, then the answer as a Markdown card. That page's CSS, markup and rendering
script live here once; each caller adds only its own poll loop, because the two
read different JSON (/events for one run vs one worker of a swarm). BASE_CSS and
BASE_JS (colours, fonts, avatars, icons, small helpers) are also what the swarm
dashboard is built on, so the three windows look like one product.

Everything an agent produced is untrusted, since a prompt-injected agent writes its
own answer. Every string therefore reaches the DOM as textContent or through md(),
which escapes first (quotes too: md() puts a link's target in a title attribute)
and only then adds its own tags. Links are shown, never made clickable. The icons
and logos are fixed SVG strings, never built from data.
"""

from __future__ import annotations

import base64
import json

# Each backend's logo, and Claude's, from @lobehub/icons-static-svg 1.95.1 (MIT).
# The marks are their owners' trademarks, shown only to say which agent is which.
# Trimmed to bare SVG: one-colour marks are filled white for the dark tiles, and
# Codex's white app-icon square is dropped so its mark sits on the same tile as
# the rest. They load as <img> data URIs, so each is an isolated document: nothing
# in one can run script, and two copies of a gradient id can never collide.
#
# The icon set's licence, which has to travel with the paths below:
#
#   MIT License
#
#   Copyright (c) 2023 LobeHub
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.
_LOGO_SVG = {
    "agy": '<svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><mask height="23" id="lobe-icons-antigravity-0-_R_0_" maskUnits="userSpaceOnUse" width="24" x="0" y="1"><path d="M21.751 22.607c1.34 1.005 3.35.335 1.508-1.508C17.73 15.74 18.904 1 12.037 1 5.17 1 6.342 15.74.815 21.1c-2.01 2.009.167 2.511 1.507 1.506 5.192-3.517 4.857-9.714 9.715-9.714 4.857 0 4.522 6.197 9.714 9.715z" fill="#fff"></path></mask><g mask="url(#lobe-icons-antigravity-0-_R_0_)"><g filter="url(#lobe-icons-antigravity-1-_R_0_)"><path d="M-1.018-3.992c-.408 3.591 2.686 6.89 6.91 7.37 4.225.48 7.98-2.043 8.387-5.633.408-3.59-2.686-6.89-6.91-7.37-4.225-.479-7.98 2.043-8.387 5.633z" fill="#FFE432"></path></g><g filter="url(#lobe-icons-antigravity-2-_R_0_)"><path d="M15.269 7.747c1.058 4.557 5.691 7.374 10.348 6.293 4.657-1.082 7.575-5.653 6.516-10.21-1.058-4.556-5.691-7.374-10.348-6.292-4.657 1.082-7.575 5.653-6.516 10.21z" fill="#FC413D"></path></g><g filter="url(#lobe-icons-antigravity-3-_R_0_)"><path d="M-12.443 10.804c1.338 4.703 7.36 7.11 13.453 5.378 6.092-1.733 9.947-6.95 8.61-11.652C8.282-.173 2.26-2.58-3.833-.848-9.925.884-13.78 6.1-12.443 10.804z" fill="#00B95C"></path></g><g filter="url(#lobe-icons-antigravity-4-_R_0_)"><path d="M-12.443 10.804c1.338 4.703 7.36 7.11 13.453 5.378 6.092-1.733 9.947-6.95 8.61-11.652C8.282-.173 2.26-2.58-3.833-.848-9.925.884-13.78 6.1-12.443 10.804z" fill="#00B95C"></path></g><g filter="url(#lobe-icons-antigravity-5-_R_0_)"><path d="M-7.608 14.703c3.352 3.424 9.126 3.208 12.896-.483 3.77-3.69 4.108-9.459.756-12.883C2.69-2.087-3.083-1.871-6.853 1.82c-3.77 3.69-4.108 9.458-.755 12.883z" fill="#00B95C"></path></g><g filter="url(#lobe-icons-antigravity-6-_R_0_)"><path d="M9.932 27.617c1.04 4.482 5.384 7.303 9.7 6.3 4.316-1.002 6.971-5.448 5.93-9.93-1.04-4.483-5.384-7.304-9.7-6.301-4.316 1.002-6.971 5.448-5.93 9.93z" fill="#3186FF"></path></g><g filter="url(#lobe-icons-antigravity-7-_R_0_)"><path d="M2.572-8.185C.392-3.329 2.778 2.472 7.9 4.771c5.122 2.3 11.042.227 13.222-4.63 2.18-4.855-.205-10.656-5.327-12.955-5.122-2.3-11.042-.227-13.222 4.63z" fill="#FBBC04"></path></g><g filter="url(#lobe-icons-antigravity-8-_R_0_)"><path d="M-3.267 38.686c-5.277-2.072 3.742-19.117 5.984-24.83 2.243-5.712 8.34-8.664 13.616-6.592 5.278 2.071 11.533 13.482 9.29 19.195-2.242 5.713-23.613 14.298-28.89 12.227z" fill="#3186FF"></path></g><g filter="url(#lobe-icons-antigravity-9-_R_0_)"><path d="M28.71 17.471c-1.413 1.649-5.1.808-8.236-1.878-3.135-2.687-4.531-6.201-3.118-7.85 1.412-1.649 5.1-.808 8.235 1.878s4.532 6.2 3.119 7.85z" fill="#749BFF"></path></g><g filter="url(#lobe-icons-antigravity-10-_R_0_)"><path d="M18.163 9.077c5.81 3.93 12.502 4.19 14.946.577 2.443-3.612-.287-9.727-6.098-13.658-5.81-3.931-12.502-4.19-14.946-.577-2.443 3.612.287 9.727 6.098 13.658z" fill="#FC413D"></path></g><g filter="url(#lobe-icons-antigravity-11-_R_0_)"><path d="M-.915 2.684c-1.44 3.473-.97 6.967 1.05 7.804 2.02.837 4.824-1.3 6.264-4.772 1.44-3.473.97-6.967-1.05-7.804-2.02-.837-4.824 1.3-6.264 4.772z" fill="#FFEE48"></path></g></g><defs><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="17.587" id="lobe-icons-antigravity-1-_R_0_" width="19.838" x="-3.288" y="-11.917"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="1.117"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="38.565" id="lobe-icons-antigravity-2-_R_0_" width="38.9" x="4.251" y="-13.493"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="5.4"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="36.517" id="lobe-icons-antigravity-3-_R_0_" width="40.955" x="-21.889" y="-10.592"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="4.591"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="36.517" id="lobe-icons-antigravity-4-_R_0_" width="40.955" x="-21.889" y="-10.592"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="4.591"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="36.595" id="lobe-icons-antigravity-5-_R_0_" width="36.632" x="-19.099" y="-10.278"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="4.591"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="34.087" id="lobe-icons-antigravity-6-_R_0_" width="33.533" x=".981" y="8.758"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="4.363"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="35.276" id="lobe-icons-antigravity-7-_R_0_" width="35.978" x="-6.143" y="-21.659"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="3.954"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="46.523" id="lobe-icons-antigravity-8-_R_0_" width="45.114" x="-11.96" y="-.46"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="3.531"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="24.054" id="lobe-icons-antigravity-9-_R_0_" width="25.094" x="10.485" y=".58"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="3.159"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="30.007" id="lobe-icons-antigravity-10-_R_0_" width="33.508" x="5.833" y="-12.467"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="2.669"></feGaussianBlur></filter><filter color-interpolation-filters="sRGB" filterUnits="userSpaceOnUse" height="26.151" id="lobe-icons-antigravity-11-_R_0_" width="22.194" x="-8.355" y="-8.876"><feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood><feBlend in="SourceGraphic" in2="BackgroundImageFix" result="shape"></feBlend><feGaussianBlur result="effect1_foregroundBlur_977_115" stdDeviation="3.303"></feGaussianBlur></filter></defs></svg>',
    "codex": '<svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M9.064 3.344a4.578 4.578 0 012.285-.312c1 .115 1.891.54 2.673 1.275.01.01.024.017.037.021a.09.09 0 00.043 0 4.55 4.55 0 013.046.275l.047.022.116.057a4.581 4.581 0 012.188 2.399c.209.51.313 1.041.315 1.595a4.24 4.24 0 01-.134 1.223.123.123 0 00.03.115c.594.607.988 1.33 1.183 2.17.289 1.425-.007 2.71-.887 3.854l-.136.166a4.548 4.548 0 01-2.201 1.388.123.123 0 00-.081.076c-.191.551-.383 1.023-.74 1.494-.9 1.187-2.222 1.846-3.711 1.838-1.187-.006-2.239-.44-3.157-1.302a.107.107 0 00-.105-.024c-.388.125-.78.143-1.204.138a4.441 4.441 0 01-1.945-.466 4.544 4.544 0 01-1.61-1.335c-.152-.202-.303-.392-.414-.617a5.81 5.81 0 01-.37-.961 4.582 4.582 0 01-.014-2.298.124.124 0 00.006-.056.085.085 0 00-.027-.048 4.467 4.467 0 01-1.034-1.651 3.896 3.896 0 01-.251-1.192 5.189 5.189 0 01.141-1.6c.337-1.112.982-1.985 1.933-2.618.212-.141.413-.251.601-.33.215-.089.43-.164.646-.227a.098.098 0 00.065-.066 4.51 4.51 0 01.829-1.615 4.535 4.535 0 011.837-1.388zm3.482 10.565a.637.637 0 000 1.272h3.636a.637.637 0 100-1.272h-3.636zM8.462 9.23a.637.637 0 00-1.106.631l1.272 2.224-1.266 2.136a.636.636 0 101.095.649l1.454-2.455a.636.636 0 00.005-.64L8.462 9.23z" fill="url(#lobe-icons-codex-_R_0_)"></path><defs><linearGradient gradientUnits="userSpaceOnUse" id="lobe-icons-codex-_R_0_" x1="12" x2="12" y1="3" y2="21"><stop stop-color="#B1A7FF"></stop><stop offset=".5" stop-color="#7A9DFF"></stop><stop offset="1" stop-color="#3941FF"></stop></linearGradient></defs></svg>',
    "copilot": '<svg width="24" height="24" fill="#fff" fill-rule="evenodd" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M19.245 5.364c1.322 1.36 1.877 3.216 2.11 5.817.622 0 1.2.135 1.592.654l.73.964c.21.278.323.61.323.955v2.62c0 .339-.173.669-.453.868C20.239 19.602 16.157 21.5 12 21.5c-4.6 0-9.205-2.583-11.547-4.258-.28-.2-.452-.53-.453-.868v-2.62c0-.345.113-.679.321-.956l.73-.963c.392-.517.974-.654 1.593-.654l.029-.297c.25-2.446.81-4.213 2.082-5.52 2.461-2.54 5.71-2.851 7.146-2.864h.198c1.436.013 4.685.323 7.146 2.864zm-7.244 4.328c-.284 0-.613.016-.962.05-.123.447-.305.85-.57 1.108-1.05 1.023-2.316 1.18-2.994 1.18-.638 0-1.306-.13-1.851-.464-.516.165-1.012.403-1.044.996a65.882 65.882 0 00-.063 2.884l-.002.48c-.002.563-.005 1.126-.013 1.69.002.326.204.63.51.765 2.482 1.102 4.83 1.657 6.99 1.657 2.156 0 4.504-.555 6.985-1.657a.854.854 0 00.51-.766c.03-1.682.006-3.372-.076-5.053-.031-.596-.528-.83-1.046-.996-.546.333-1.212.464-1.85.464-.677 0-1.942-.157-2.993-1.18-.266-.258-.447-.661-.57-1.108-.32-.032-.64-.049-.96-.05zm-2.525 4.013c.539 0 .976.426.976.95v1.753c0 .525-.437.95-.976.95a.964.964 0 01-.976-.95v-1.752c0-.525.437-.951.976-.951zm5 0c.539 0 .976.426.976.95v1.753c0 .525-.437.95-.976.95a.964.964 0 01-.976-.95v-1.752c0-.525.437-.951.976-.951zM7.635 5.087c-1.05.102-1.935.438-2.385.906-.975 1.037-.765 3.668-.21 4.224.405.394 1.17.657 1.995.657h.09c.649-.013 1.785-.176 2.73-1.11.435-.41.705-1.433.675-2.47-.03-.834-.27-1.52-.63-1.813-.39-.336-1.275-.482-2.265-.394zm6.465.394c-.36.292-.6.98-.63 1.813-.03 1.037.24 2.06.675 2.47.968.957 2.136 1.104 2.776 1.11h.044c.825 0 1.59-.263 1.995-.657.555-.556.765-3.187-.21-4.224-.45-.468-1.335-.804-2.385-.906-.99-.088-1.875.058-2.265.394zM12 7.615c-.24 0-.525.015-.84.044.03.16.045.336.06.526l-.001.159a2.94 2.94 0 01-.014.25c.225-.022.425-.027.612-.028h.366c.187 0 .387.006.612.028-.015-.146-.015-.277-.015-.409.015-.19.03-.365.06-.526a9.29 9.29 0 00-.84-.044z"></path></svg>',
    "cursor": '<svg width="24" height="24" fill="#fff" fill-rule="evenodd" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z"></path></svg>',
    "grok": '<svg width="24" height="24" fill="#fff" fill-rule="evenodd" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M9.27 15.29l7.978-5.897c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.383.06-.082.12-.164.179-.248l-3.301 3.305v-.01L9.267 15.292M7.623 16.723c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.536-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199 1.259-1.682 1.925l7.62-6.815"></path></svg>',
    "opencode": '<svg width="24" height="24" fill="#fff" fill-rule="evenodd" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M16 6H8v12h8V6zm4 16H4V2h16v20z"></path></svg>',
    "muse": '<svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M6.897 4h-.024l-.031 2.615h.022c1.715 0 3.046 1.357 5.94 6.246l.175.297.012.02 1.62-2.438-.012-.019a48.763 48.763 0 00-1.098-1.716 28.01 28.01 0 00-1.175-1.629C10.413 4.932 8.812 4 6.896 4z" fill="url(#lobe-icons-meta-0-_R_0_)"></path><path d="M6.873 4C4.95 4.01 3.247 5.258 2.02 7.17a4.352 4.352 0 00-.01.017l2.254 1.231.011-.017c.718-1.083 1.61-1.774 2.568-1.785h.021L6.896 4h-.023z" fill="url(#lobe-icons-meta-1-_R_0_)"></path><path d="M2.019 7.17l-.011.017C1.2 8.447.598 9.995.274 11.664l-.005.022 2.534.6.004-.022c.27-1.467.786-2.828 1.456-3.845l.011-.017L2.02 7.17z" fill="url(#lobe-icons-meta-2-_R_0_)"></path><path d="M2.807 12.264l-2.533-.6-.005.022c-.177.918-.267 1.851-.269 2.786v.023l2.598.233v-.023a12.591 12.591 0 01.21-2.44z" fill="url(#lobe-icons-meta-3-_R_0_)"></path><path d="M2.677 15.537a5.462 5.462 0 01-.079-.813v-.022L0 14.468v.024a8.89 8.89 0 00.146 1.652l2.535-.585a4.106 4.106 0 01-.004-.022z" fill="url(#lobe-icons-meta-4-_R_0_)"></path><path d="M3.27 16.89c-.284-.31-.484-.756-.589-1.328l-.004-.021-2.535.585.004.021c.192 1.01.568 1.85 1.106 2.487l.014.017 2.018-1.745a2.106 2.106 0 01-.015-.016z" fill="url(#lobe-icons-meta-5-_R_0_)"></path><path d="M10.78 9.654c-1.528 2.35-2.454 3.825-2.454 3.825-2.035 3.2-2.739 3.917-3.871 3.917a1.545 1.545 0 01-1.186-.508l-2.017 1.744.014.017C2.01 19.518 3.058 20 4.356 20c1.963 0 3.374-.928 5.884-5.33l1.766-3.13a41.283 41.283 0 00-1.227-1.886z" fill="#0082FB"></path><path d="M13.502 5.946l-.016.016c-.4.43-.786.908-1.16 1.416.378.483.768 1.024 1.175 1.63.48-.743.928-1.345 1.367-1.807l.016-.016-1.382-1.24z" fill="url(#lobe-icons-meta-6-_R_0_)"></path><path d="M20.918 5.713C19.853 4.633 18.583 4 17.225 4c-1.432 0-2.637.787-3.723 1.944l-.016.016 1.382 1.24.016-.017c.715-.747 1.408-1.12 2.176-1.12.826 0 1.6.39 2.27 1.075l.015.016 1.589-1.425-.016-.016z" fill="#0082FB"></path><path d="M23.998 14.125c-.06-3.467-1.27-6.566-3.064-8.396l-.016-.016-1.588 1.424.015.016c1.35 1.392 2.277 3.98 2.361 6.971v.023h2.292v-.022z" fill="url(#lobe-icons-meta-7-_R_0_)"></path><path d="M23.998 14.15v-.023h-2.292v.022c.004.14.006.282.006.424 0 .815-.121 1.474-.368 1.95l-.011.022 1.708 1.782.013-.02c.62-.96.946-2.293.946-3.91 0-.083 0-.165-.002-.247z" fill="url(#lobe-icons-meta-8-_R_0_)"></path><path d="M21.344 16.52l-.011.02c-.214.402-.519.67-.917.787l.778 2.462a3.493 3.493 0 00.438-.182 3.558 3.558 0 001.366-1.218l.044-.065.012-.02-1.71-1.784z" fill="url(#lobe-icons-meta-9-_R_0_)"></path><path d="M19.92 17.393c-.262 0-.492-.039-.718-.14l-.798 2.522c.449.153.927.222 1.46.222.492 0 .943-.073 1.352-.215l-.78-2.462c-.167.05-.341.075-.517.073z" fill="url(#lobe-icons-meta-10-_R_0_)"></path><path d="M18.323 16.534l-.014-.017-1.836 1.914.016.017c.637.682 1.246 1.105 1.937 1.337l.797-2.52c-.291-.125-.573-.353-.9-.731z" fill="url(#lobe-icons-meta-11-_R_0_)"></path><path d="M18.309 16.515c-.55-.642-1.232-1.712-2.303-3.44l-1.396-2.336-.011-.02-1.62 2.438.012.02.989 1.668c.959 1.61 1.74 2.774 2.493 3.585l.016.016 1.834-1.914a2.353 2.353 0 01-.014-.017z" fill="url(#lobe-icons-meta-12-_R_0_)"></path><defs><linearGradient id="lobe-icons-meta-0-_R_0_" x1="75.897%" x2="26.312%" y1="89.199%" y2="12.194%"><stop offset=".06%" stop-color="#0867DF"></stop><stop offset="45.39%" stop-color="#0668E1"></stop><stop offset="85.91%" stop-color="#0064E0"></stop></linearGradient><linearGradient id="lobe-icons-meta-1-_R_0_" x1="21.67%" x2="97.068%" y1="75.874%" y2="23.985%"><stop offset="13.23%" stop-color="#0064DF"></stop><stop offset="99.88%" stop-color="#0064E0"></stop></linearGradient><linearGradient id="lobe-icons-meta-2-_R_0_" x1="38.263%" x2="60.895%" y1="89.127%" y2="16.131%"><stop offset="1.47%" stop-color="#0072EC"></stop><stop offset="68.81%" stop-color="#0064DF"></stop></linearGradient><linearGradient id="lobe-icons-meta-3-_R_0_" x1="47.032%" x2="52.15%" y1="90.19%" y2="15.745%"><stop offset="7.31%" stop-color="#007CF6"></stop><stop offset="99.43%" stop-color="#0072EC"></stop></linearGradient><linearGradient id="lobe-icons-meta-4-_R_0_" x1="52.155%" x2="47.591%" y1="58.301%" y2="37.004%"><stop offset="7.31%" stop-color="#007FF9"></stop><stop offset="100%" stop-color="#007CF6"></stop></linearGradient><linearGradient id="lobe-icons-meta-5-_R_0_" x1="37.689%" x2="61.961%" y1="12.502%" y2="63.624%"><stop offset="7.31%" stop-color="#007FF9"></stop><stop offset="100%" stop-color="#0082FB"></stop></linearGradient><linearGradient id="lobe-icons-meta-6-_R_0_" x1="34.808%" x2="62.313%" y1="68.859%" y2="23.174%"><stop offset="27.99%" stop-color="#007FF8"></stop><stop offset="91.41%" stop-color="#0082FB"></stop></linearGradient><linearGradient id="lobe-icons-meta-7-_R_0_" x1="43.762%" x2="57.602%" y1="6.235%" y2="98.514%"><stop offset="0%" stop-color="#0082FB"></stop><stop offset="99.95%" stop-color="#0081FA"></stop></linearGradient><linearGradient id="lobe-icons-meta-8-_R_0_" x1="60.055%" x2="39.88%" y1="4.661%" y2="69.077%"><stop offset="6.19%" stop-color="#0081FA"></stop><stop offset="100%" stop-color="#0080F9"></stop></linearGradient><linearGradient id="lobe-icons-meta-9-_R_0_" x1="30.282%" x2="61.081%" y1="59.32%" y2="33.244%"><stop offset="0%" stop-color="#027AF3"></stop><stop offset="100%" stop-color="#0080F9"></stop></linearGradient><linearGradient id="lobe-icons-meta-10-_R_0_" x1="20.433%" x2="82.112%" y1="50.001%" y2="50.001%"><stop offset="0%" stop-color="#0377EF"></stop><stop offset="99.94%" stop-color="#0279F1"></stop></linearGradient><linearGradient id="lobe-icons-meta-11-_R_0_" x1="40.303%" x2="72.394%" y1="35.298%" y2="57.811%"><stop offset=".19%" stop-color="#0471E9"></stop><stop offset="100%" stop-color="#0377EF"></stop></linearGradient><linearGradient id="lobe-icons-meta-12-_R_0_" x1="32.254%" x2="68.003%" y1="19.719%" y2="84.908%"><stop offset="27.65%" stop-color="#0867DF"></stop><stop offset="100%" stop-color="#0471E9"></stop></linearGradient></defs></svg>',
    "claude": '<svg width="24" height="24" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z" fill="#D97757" fill-rule="nonzero"></path></svg>',
}


def _logo_uris() -> str:
    return json.dumps(
        {
            k: "data:image/svg+xml;base64," + base64.b64encode(v.encode("utf-8")).decode("ascii")
            for k, v in _LOGO_SVG.items()
        }
    )


BASE_CSS = r"""
:root{
 --bg:#0a0c11;--s1:#10131a;--s2:#151922;--s3:#1c212c;--bd:#232936;--bd2:#1a1e28;
 --tx:#e7eaf0;--tx2:#a6aebb;--tx3:#687182;
 --green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--amber:#f5b94a;
 --sans:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,"Inter",Roboto,"Helvetica Neue",Arial,sans-serif;
 --mono:"Cascadia Code","Cascadia Mono",ui-monospace,SFMono-Regular,Consolas,"DejaVu Sans Mono",monospace;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg)}
body{color:var(--tx);font:13.5px/1.55 var(--sans);-webkit-font-smoothing:antialiased;background:radial-gradient(900px 320px at 50% -150px,rgba(63,223,127,.09),transparent 70%) fixed,var(--bg)}
[hidden]{display:none!important}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#262c38;border-radius:8px;border:2px solid var(--bg)}
svg{display:block}
.av{flex:none;width:26px;height:26px;border-radius:8px;display:grid;place-items:center;font:700 10.5px/1 var(--sans);color:var(--tx2);background:linear-gradient(180deg,#262c3a,#181c25);border:1px solid rgba(255,255,255,.09);box-shadow:0 3px 10px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.05)}
.av img{width:64%;height:64%;display:block;-webkit-user-drag:none}
.av.codex img{width:82%;height:82%}
.av.big{width:34px;height:34px;border-radius:10px;font-size:12.5px}
.av.claude{background:linear-gradient(180deg,#2c2320,#1d1716);border-color:rgba(217,119,87,.28)}
.av.ghost{visibility:hidden}
.ring{display:inline-block;flex:none;width:12px;height:12px;border-radius:50%;border:2px solid currentColor;border-right-color:transparent;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes rise{from{opacity:0;transform:translateY(8px)}}
@keyframes pop{0%{transform:scale(.5);opacity:.2}60%{transform:scale(1.18)}100%{transform:scale(1)}}
.repo{flex:none;font:500 10.5px/1.7 var(--mono);color:#9ee9ba;background:rgba(63,223,127,.08);border:1px solid rgba(63,223,127,.22);border-radius:6px;padding:0 6px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
kbd{font:600 10px/1 var(--mono);background:var(--s2);border:1px solid var(--bd);border-bottom-width:2px;border-radius:5px;padding:2px 5px;color:var(--tx2)}
"""

CSS = r"""
.top{position:sticky;top:0;z-index:5;background:rgba(10,12,17,.84);backdrop-filter:blur(10px);border-bottom:1px solid var(--bd2)}
header{display:flex;align-items:center;gap:11px;padding:11px 16px 10px}
.ttl{flex:1;min-width:0}
.name{font-weight:650;font-size:14px;letter-spacing:.1px}
.subt{display:flex;align-items:center;gap:7px;min-width:0;color:var(--tx3);font-size:11.5px}
.chip{flex:none;display:flex;align-items:center;gap:7px;padding:5px 12px 5px 10px;border-radius:999px;font-size:12px;font-weight:600;background:var(--s2);border:1px solid var(--bd);color:var(--tx2);font-variant-numeric:tabular-nums;white-space:nowrap;transition:background .3s,border-color .3s,color .3s}
.chip .budget{font-weight:400;color:var(--tx3)}
.chip .ic svg{width:13px;height:13px}.chip .ic svg,.chip .ic .ring{animation:pop .4s ease}
.chip .ic .ring{width:11px;height:11px;animation:spin .8s linear infinite}
.chip.working{color:#c3f7d8;background:rgba(63,223,127,.09);border-color:rgba(63,223,127,.3)}
.chip.done{color:#c3f3f9;background:rgba(92,214,230,.09);border-color:rgba(92,214,230,.32)}
.chip.error{color:#ffc9c9;background:rgba(255,107,107,.1);border-color:rgba(255,107,107,.34)}
.chip.lost{color:#ffe2ab;background:rgba(245,185,74,.1);border-color:rgba(245,185,74,.34)}
.track{height:3px}
.gfill{height:100%;width:0;border-radius:0 3px 3px 0;background:linear-gradient(90deg,var(--green),var(--cyan));box-shadow:0 0 12px rgba(63,223,127,.45);transition:width .6s cubic-bezier(.2,.7,.2,1)}
.gfill.warn{background:linear-gradient(90deg,var(--green),var(--amber));box-shadow:0 0 12px rgba(245,185,74,.45)}
.gfill.error{background:var(--red);box-shadow:0 0 12px rgba(255,107,107,.45)}
#chat{max-width:880px;margin:0 auto;padding:20px 16px 64px;display:flex;flex-direction:column;gap:14px}
.empty{margin:22vh auto 0;display:flex;flex-direction:column;align-items:center;gap:12px;color:var(--tx3);font-size:13px}
.empty .ring{width:22px;height:22px}
.msg{display:flex;gap:10px;align-items:flex-start;animation:rise .35s cubic-bezier(.2,.7,.2,1) both}
.msg.user{flex-direction:row-reverse}
.col{display:flex;flex-direction:column;gap:5px;min-width:0;max-width:calc(100% - 36px)}
.msg.user .col{align-items:flex-end;max-width:84%}
.msg.bot .col{flex:1}
.who{font-size:11px;font-weight:600;color:var(--tx3);padding:0 4px}
.bubble{padding:10px 14px;border-radius:16px;border-top-right-radius:6px;word-break:break-word;min-width:0;background:linear-gradient(180deg,#16291f,#112119);border:1px solid rgba(63,223,127,.24);color:#e8f6ee;box-shadow:0 6px 20px rgba(0,0,0,.25)}
.btext{white-space:pre-wrap}
.clampable .btext{max-height:7.6em;overflow:hidden;-webkit-mask-image:linear-gradient(180deg,#000 68%,transparent)}
.expanded .btext{max-height:60vh;overflow:auto;-webkit-mask-image:none}
.exp{margin-top:6px;font-size:11.5px;font-weight:600;color:var(--green);cursor:pointer;user-select:none;opacity:.85}
.exp:hover{opacity:1}
.card,.trace{background:var(--s1);border:1px solid var(--bd);border-radius:14px;border-top-left-radius:6px;box-shadow:0 6px 24px rgba(0,0,0,.26);min-width:0;overflow:hidden}
.card.err{border-color:rgba(255,107,107,.36);background:linear-gradient(180deg,#1b1215,#140e11)}
.card-h{display:flex;align-items:center;gap:8px;padding:7px 8px 7px 14px;border-bottom:1px solid var(--bd2);font-size:11.5px;color:var(--tx3)}
.card-h .lbl{font-weight:650;color:var(--tx2)}
.card-h .ic{color:var(--red)}.card-h .ic svg{width:13px;height:13px}
.card.err .card-h{border-color:rgba(255,107,107,.16)}.card.err .card-h .lbl{color:#ffb8b8}
.card-b{padding:11px 15px 13px}
.btn{margin-left:auto;display:inline-flex;align-items:center;gap:5px;background:transparent;border:1px solid var(--bd);color:var(--tx3);font:600 11px/1 var(--sans);padding:5px 9px;border-radius:7px;cursor:pointer;transition:color .15s,border-color .15s,background .15s}
.btn:hover{color:var(--tx);border-color:#3a4252;background:var(--s2)}
.btn svg{width:12px;height:12px}
.btn.ok{color:var(--green);border-color:rgba(63,223,127,.4)}
.trace.bad{border-color:rgba(255,107,107,.32)}
.trace-head{display:flex;align-items:center;gap:9px;padding:9px 12px 9px 14px;cursor:pointer;user-select:none;font-size:12px;color:var(--tx2)}
.trace-head:hover{background:var(--s2)}
.trace-head .ic{color:var(--green);display:grid;place-items:center}.trace-head .ic svg{width:14px;height:14px;animation:pop .4s ease}
.trace-head .ic .ring{width:11px;height:11px}
.trace.bad .trace-head .ic{color:var(--red)}
.tlabel{font-weight:650;color:var(--tx)}.tmeta{color:var(--tx3)}
.chev{margin-left:auto;color:var(--tx3);transition:transform .2s}.chev svg{width:14px;height:14px}
.trace.collapsed .chev{transform:rotate(-90deg)}
.trace-body{padding:6px 14px 10px;border-top:1px solid var(--bd2)}
.trace.collapsed .trace-body{display:none}
.wait{display:flex;gap:9px;align-items:center;color:var(--tx3);font-size:12px;padding:6px 0 2px}
.wait .ring{width:11px;height:11px}
.step{position:relative;display:grid;grid-template-columns:42px 14px minmax(0,1fr) auto;column-gap:9px;align-items:start;padding:4px 0;animation:rise .3s ease both}
.step::before{content:"";position:absolute;left:57.5px;top:0;bottom:0;width:1px;background:var(--bd)}
.step:first-child::before{top:14px}.step:last-child::before{bottom:auto;height:14px}
.step:only-child::before{display:none}
.ts{font:10.5px/20px var(--mono);color:var(--tx3);text-align:right;font-variant-numeric:tabular-nums}
.node{position:relative;z-index:1;width:9px;height:9px;margin:5.5px auto 0;border-radius:50%;background:var(--s1);border:2px solid var(--tx3)}
.step.narration .node{border-color:var(--cyan)}
.step.command .node{border-color:var(--green);background:var(--green)}
.step.run .node{background:var(--s1);animation:beat 1.1s ease-in-out infinite}
@keyframes beat{50%{box-shadow:0 0 0 5px rgba(63,223,127,.16)}}
.step.fail .node,.step.bad .node{border-color:var(--red);background:var(--red)}
.txt{min-width:0;font-size:12.5px;line-height:20px;word-break:break-word;white-space:pre-wrap;color:var(--tx2)}
.cmd{font:11.5px/20px var(--mono);color:var(--tx);background:#0b0e14;border:1px solid var(--bd);border-radius:6px;padding:1px 7px;-webkit-box-decoration-break:clone;box-decoration-break:clone}
.cmd b{color:var(--green);font-weight:600;margin-right:7px}
.step.fail .cmd{border-color:rgba(255,107,107,.35)}
.step.result .txt{color:var(--tx3)}.step.bad .txt{color:#ffb8b8}
.mark{display:flex;align-items:center;gap:4px;font:10.5px/20px var(--mono);white-space:nowrap;color:var(--tx3);min-height:20px}
.mark svg{width:12px;height:12px;animation:pop .35s ease}.mark .ring{width:10px;height:10px;color:var(--green)}
.step.ok .mark{color:var(--green)}.step.fail .mark{color:var(--red)}
.serr{grid-column:3/-1;margin-top:5px;padding:5px 9px;border-radius:7px;background:rgba(255,107,107,.08);border:1px solid rgba(255,107,107,.2);color:#ffb8b8;font:11px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
.md{line-height:1.62}
.md>:first-child{margin-top:0!important}.md>:last-child{margin-bottom:0!important}
.md .h{font-weight:700;margin:16px 0 6px;color:#fff;line-height:1.3}
.md .h1{font-size:18px}.md .h2{font-size:16px}.md .h3{font-size:14.5px;color:#d3f6e1}.md .h4,.md .h5,.md .h6{font-size:13.5px;color:var(--tx2)}
.md .p{margin:6px 0;white-space:pre-wrap;word-break:break-word}
.md .li{display:flex;gap:9px;margin:4px 0}
.md .li.l1{margin-left:20px}.md .li.l2{margin-left:40px}.md .li.l3{margin-left:60px}
.md .bul{flex:none;min-width:14px;text-align:right;color:var(--green);font-weight:700}
.md .lit{min-width:0;white-space:pre-wrap;word-break:break-word}
.md .bq{margin:9px 0;padding:7px 12px;border-left:3px solid var(--cyan);background:rgba(92,214,230,.06);border-radius:0 9px 9px 0;color:var(--tx2);white-space:pre-wrap}
.md .hr{border:0;border-top:1px solid var(--bd);margin:14px 0}
.md code{font:12px var(--mono);background:#1a1f2a;border:1px solid #262c39;padding:1px 5px;border-radius:5px;color:#a8ebbc}
.md .cb{margin:10px 0;border:1px solid var(--bd);border-radius:10px;background:#07090d;overflow:hidden}
.md .cbh{display:flex;align-items:center;gap:8px;padding:4px 5px 4px 12px;font:650 10px/1 var(--sans);letter-spacing:.6px;text-transform:uppercase;color:var(--tx3);background:#0d1017;border-bottom:1px solid var(--bd2)}
.md pre.code{margin:0;padding:11px 13px;overflow:auto;font:12px/1.6 var(--mono);color:#e4ece6;white-space:pre}
.md .tw{overflow:auto;margin:10px 0;border:1px solid var(--bd);border-radius:10px}
.md table{border-collapse:collapse;width:100%;font-size:12.5px}
.md th,.md td{padding:6px 11px;text-align:left;vertical-align:top;border-bottom:1px solid var(--bd2)}
.md th{background:var(--s2);color:var(--tx2);font-weight:650;font-size:11.5px}
.md tr:last-child td{border-bottom:0}.md tr:nth-child(odd) td{background:rgba(255,255,255,.018)}
.md .lnk{color:var(--cyan);text-decoration:underline;text-decoration-color:rgba(92,214,230,.4);text-underline-offset:2px;cursor:help}
.md strong{color:#fff;font-weight:650}.md em{color:#eef0f4}.md del{opacity:.55}
.card.err .md strong{color:#ffd6d6}
.shot{align-self:flex-start;max-width:100%;border-radius:14px;border:1px solid var(--bd);box-shadow:0 10px 30px rgba(0,0,0,.4);display:block}
.jump{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:6px;background:rgba(21,25,34,.94);backdrop-filter:blur(8px);border:1px solid #2c3342;color:var(--tx);font:600 12px var(--sans);padding:7px 14px;border-radius:999px;cursor:pointer;box-shadow:0 8px 24px rgba(0,0,0,.45);z-index:6;animation:rise .3s ease}
.jump svg{width:13px;height:13px;color:var(--green)}
"""

# #av, #bkname and #repo are filled by setAgent from the page's poll loop.
BODY = r"""
<div class="top">
 <header>
  <div class="av big" id="av"></div>
  <div class="ttl"><div class="name">Agent Intern</div>
   <div class="subt"><span id="bkname">connecting…</span><span class="repo" id="repo" hidden></span></div></div>
  <div class="chip" id="chip"><span class="ic" id="chipic"></span><span id="st">Connecting</span><span class="budget" id="budget"></span></div>
 </header>
 <div class="track" id="track"><div class="gfill" id="gfill"></div></div>
</div>
<div id="chat"></div>
<button class="jump" id="jump" hidden><svg viewBox="0 0 16 16"><path d="M8 3v10m-4-4l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>Jump to latest</button>
"""

BASE_JS = r"""
const $=id=>document.getElementById(id);
const Q=new URLSearchParams(location.search);
const K=encodeURIComponent(Q.get("k")||"");
const BK={antigravity:"agy",agy:"agy",codex:"codex",copilot:"copilot",cursor:"cursor",grok:"grok",opencode:"opencode",muse:"muse"};
const MONO={agy:"Ag",codex:"Cx",copilot:"Cp",cursor:"Cu",grok:"Gk",opencode:"Oc",muse:"Mu"};
const LOGO=__LOGOS__;
const NAMES={agy:"Antigravity",codex:"Codex",copilot:"GitHub Copilot",cursor:"Cursor",grok:"Grok",opencode:"opencode",muse:"Muse",claude:"Claude"};
function bkName(b){return BK[b]||"agy";}
function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");}
function fmtS(s){s=Math.max(0,+s||0);if(s<60)return s.toFixed(1)+"s";return Math.floor(s/60)+"m"+String(Math.floor(s%60)).padStart(2,"0")+"s";}
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
const SV=(d,extra)=>'<svg viewBox="0 0 16 16"'+(extra||"")+'>'+d+'</svg>';
const ST='fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"';
const IC={
 check:SV('<path d="M3.5 8.5l3 3 6-7" '+ST+' stroke-width="2"/>'),
 x:SV('<path d="M4.5 4.5l7 7m0-7l-7 7" '+ST+' stroke-width="2"/>'),
 chev:SV('<path d="M4.5 6.5l3.5 3.5 3.5-3.5" '+ST+' stroke-width="1.8"/>'),
 copy:SV('<rect x="5.5" y="5.5" width="8" height="8" rx="2" '+ST+' stroke-width="1.5"/><path d="M10.5 3.5A1.5 1.5 0 0 0 9 2H4a1.5 1.5 0 0 0-1.5 1.5v5A1.5 1.5 0 0 0 4 10" '+ST+' stroke-width="1.5"/>'),
 warn:SV('<path d="M8 2.5l6 11H2z" '+ST+' stroke-width="1.5"/><path d="M8 7v2.8" '+ST+' stroke-width="1.6"/><circle cx="8" cy="11.8" r=".9" fill="currentColor"/>'),
 clock:SV('<circle cx="8" cy="8" r="5.6" '+ST+' stroke-width="1.5"/><path d="M8 5v3.2l2.1 1.4" '+ST+' stroke-width="1.5"/>'),
};
const RING="<span class='ring'></span>";
// A logo tile for backend `b` (or "claude"), falling back to a monogram.
function paintAvatar(a,n){
 a.textContent="";a.title=NAMES[n]||n;
 if(LOGO[n]){const i=el("img");i.src=LOGO[n];i.alt="";a.appendChild(i);}else a.textContent=MONO[n]||"?";
}
function avatar(b,big){const n=b==="claude"?b:bkName(b),a=el("div","av "+n+(big?" big":""));paintAvatar(a,n);return a;}
function copyText(txt,btn){
 navigator.clipboard.writeText(txt).then(()=>{
  const s=btn.querySelector("span")||btn,o=s.textContent;
  s.textContent="Copied";btn.classList.add("ok");
  setTimeout(()=>{s.textContent=o;btn.classList.remove("ok");},1300);
 }).catch(()=>{});
}
"""

BASE_JS = BASE_JS.replace("__LOGOS__", _logo_uris())

# The rendering half. A page supplies the poll loop that feeds it: setAgent, then
# resetChat / userBubble / newTrace when a run starts, addStep per event, then
# finishTrace and botCard, with setState driving the header on every poll.
JS = r"""
// "result" texts that only mean the command before them finished fine; anything
// else a backend reports as a result is a failure, except codex's file changes.
const OK_RE=/^(done|command finished)$/i,INFO_RE=/^file change/i,BARE_FAIL=/^(failed|tool failed|error)$/i;
const COPY_BTN=cls=>"<button class='btn "+cls+"'>"+IC.copy+"<span>Copy</span></button>";
document.addEventListener("click",e=>{
 const b=e.target.closest&&e.target.closest(".ccopy");
 if(b){const p=b.closest(".cb").querySelector("pre");if(p)copyText(p.textContent,b);}
});

// ---- follow the bottom unless the reader scrolled up
let follow=true;
function toBottom(){window.scrollTo(0,document.body.scrollHeight);}
function maybeBottom(){if(follow)toBottom();}
window.addEventListener("scroll",()=>{follow=window.innerHeight+window.scrollY>=document.body.scrollHeight-44;$("jump").hidden=follow;});
$("jump").onclick=()=>{follow=true;$("jump").hidden=true;toBottom();};

// ---- markdown (input is escaped before any tag is added)
function inl(s){
 return s.split(/(`[^`]+`)/).map((p,i)=>i%2?"<code>"+p.slice(1,-1)+"</code>":p
  .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,"<span class='lnk' title='$2'>$1</span>")
  .replace(/\*\*([^*]+)\*\*/g,"<strong>$1</strong>")
  .replace(/(^|[\s(])\*([^*\s](?:[^*]*[^*\s])?)\*(?=$|[\s.,;:!?)])/g,"$1<em>$2</em>")
  .replace(/~~([^~]+)~~/g,"<del>$1</del>")).join("");
}
const TSEP=/^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
function cells(ln){return ln.trim().replace(/^\|/,"").replace(/\|$/,"").split("|").map(c=>inl(c.trim()));}
function li(ind,bul,txt){
 const d=Math.min(3,Math.floor(ind.replace(/\t/g,"  ").length/2));
 return "<div class='li"+(d?" l"+d:"")+"'><span class='bul'>"+bul+"</span><span class='lit'>"+inl(txt)+"</span></div>";
}
function md(src){
 const L=esc(src).split("\n"),out=[];let i=0;
 while(i<L.length){
  const ln=L[i];
  const f=ln.match(/^\s*```\s*([\w+#.-]*)/);
  if(f){
   const code=[];i++;
   while(i<L.length&&!/^\s*```\s*$/.test(L[i]))code.push(L[i++]);
   i++;
   out.push("<div class='cb'><div class='cbh'><span>"+(f[1]||"code")+"</span>"+COPY_BTN("ccopy")+
    "</div><pre class='code'>"+code.join("\n")+"</pre></div>");
   continue;
  }
  if(/^\s*\|.*\|\s*$/.test(ln)&&i+1<L.length&&TSEP.test(L[i+1])){
   const head=cells(ln),rows=[];i+=2;
   while(i<L.length&&/^\s*\|/.test(L[i]))rows.push(cells(L[i++]));
   out.push("<div class='tw'><table><tr>"+head.map(c=>"<th>"+c+"</th>").join("")+"</tr>"+
    rows.map(r=>"<tr>"+r.map(c=>"<td>"+c+"</td>").join("")+"</tr>").join("")+"</table></div>");
   continue;
  }
  i++;
  const h=ln.match(/^(#{1,6})\s+(.*)$/);
  if(h){out.push("<div class='h h"+h[1].length+"'>"+inl(h[2])+"</div>");continue;}
  if(/^\s*([-*_])(\s*\1){2,}\s*$/.test(ln)){out.push("<hr class='hr'>");continue;}
  const q=ln.match(/^\s*&gt;\s?(.*)$/);
  if(q){out.push("<div class='bq'>"+inl(q[1])+"</div>");continue;}
  const b=ln.match(/^(\s*)[-*+]\s+(.*)$/);
  if(b){out.push(li(b[1],"•",b[2]));continue;}
  const n=ln.match(/^(\s*)(\d+)[.)]\s+(.*)$/);
  if(n){out.push(li(n[1],n[2]+".",n[3]));continue;}
  if(ln.trim()==="")continue;
  out.push("<div class='p'>"+inl(ln)+"</div>");
 }
 return out.join("");
}

// ---- conversation. Claude's prompts sit on the right; the agent's trace and
// answers on the left, its avatar shown once per run of consecutive messages.
let AGENT="agy",TITLE="Agent Intern";
function resetChat(){
 $("chat").innerHTML="";follow=true;$("jump").hidden=true;
 traceEl=traceBody=null;pending=[];nSteps=nCmds=0;
}
function emptyState(text){
 resetChat();const e=el("div","empty");e.innerHTML=RING;e.appendChild(el("div",null,text));$("chat").appendChild(e);
}
function row(side){
 const prev=$("chat").lastElementChild,m=el("div","msg "+side);let av;
 if(side==="user")av=avatar("claude");
 else{av=avatar(AGENT);if(prev&&prev.classList.contains("bot"))av.classList.add("ghost");}
 const col=el("div","col");m.appendChild(av);m.appendChild(col);$("chat").appendChild(m);return col;
}
// A prompt as a right-aligned bubble; a long one clamps behind "Show more".
function userBubble(text,who){
 const col=row("user");
 if(who)col.appendChild(el("div","who",who));
 const b=el("div","bubble clampable"),t=el("div","btext",text||"");
 b.appendChild(t);col.appendChild(b);
 requestAnimationFrame(()=>{
  if(t.scrollHeight>t.clientHeight+2){
   const x=el("div","exp","Show more");
   x.onclick=()=>{const e=b.classList.toggle("expanded");x.textContent=e?"Show less":"Show more";maybeBottom();};
   b.appendChild(x);
  }else b.classList.remove("clampable");
  maybeBottom();
 });
}
// An answer as a Markdown card. opts: {copy, err, meta}.
function botCard(text,label,opts){
 opts=opts||{};
 const col=row("bot"),c=el("div","card"+(opts.err?" err":"")),h=el("div","card-h");
 if(opts.err){const i=el("span","ic");i.innerHTML=IC.x;h.appendChild(i);}
 h.appendChild(el("span","lbl",label||""));
 if(opts.meta)h.appendChild(el("span","meta",opts.meta));
 if(opts.copy){const cp=el("button","btn");cp.innerHTML=IC.copy+"<span>Copy</span>";cp.onclick=()=>copyText(text,cp);h.appendChild(cp);}
 const b=el("div","card-b md");b.innerHTML=md(text||"");
 c.appendChild(h);c.appendChild(b);col.appendChild(c);maybeBottom();
}
function imageCard(src){const im=el("img","shot");im.onload=maybeBottom;im.src=src;row("bot").appendChild(im);}

// ---- the live step timeline. A command stays "running" until the backend's
// next result settles it (with its duration); codex reports commands only once
// they have finished, so its commands are settled on arrival.
let traceEl=null,traceBody=null,pending=[],nSteps=0,nCmds=0,settleOnArrival=false;
function plural(n,w){return n+" "+w+(n===1?"":"s");}
function newTrace(commandsArriveFinished){
 settleOnArrival=!!commandsArriveFinished;
 const tr=el("div","trace");
 tr.innerHTML="<div class='trace-head'><span class='ic'>"+RING+"</span><span class='tlabel'>Working</span>"+
  "<span class='tmeta'></span><span class='chev'>"+IC.chev+"</span></div>"+
  "<div class='trace-body'><div class='wait'>"+RING+"Waiting for the first step…</div></div>";
 tr.querySelector(".trace-head").onclick=()=>tr.classList.toggle("collapsed");
 row("bot").appendChild(tr);
 traceEl=tr;traceBody=tr.querySelector(".trace-body");
}
function stepRow(cls,e){
 const r=el("div","step "+cls),t=el("span","txt");
 r.appendChild(el("span","ts",e.t!=null?fmtS(e.t):""));
 r.appendChild(el("span","node"));
 if(/^command/.test(cls)){const c=el("span","cmd");c.appendChild(el("b",null,"$"));c.appendChild(document.createTextNode(String(e.text||"")));t.appendChild(c);}
 else t.textContent=String(e.text||"");
 r.appendChild(t);r.appendChild(el("span","mark"));
 r._t=e.t;traceBody.appendChild(r);return r;
}
function settle(r,how,t,detail){
 r.classList.remove("run");
 const m=r.querySelector(".mark");m.innerHTML="";
 if(how){
  r.classList.add(how);m.innerHTML=how==="fail"?IC.x:IC.check;
  const dt=t!=null&&r._t!=null?t-r._t:0;
  if(how==="ok"&&dt>=0.1)m.appendChild(document.createTextNode(fmtS(dt)));
 }
 if(detail)r.appendChild(el("div","serr",detail));
}
function addStep(e){
 if(!traceBody)return;
 const w=traceBody.querySelector(".wait");if(w)w.remove();
 const txt=String(e.text||"");
 if(e.kind==="result"){
  const ok=OK_RE.test(txt),info=INFO_RE.test(txt);
  if(!info&&pending.length){settle(pending.shift(),ok?"ok":"fail",e.t,ok||BARE_FAIL.test(txt)?"":txt);return;}
  if(ok)return;
  stepRow(info?"result":"result bad",e);
 }else{
  const r=stepRow(e.kind==="command"?"command":"narration",e);nSteps++;
  if(e.kind==="command"){nCmds++;if(!settleOnArrival){r.classList.add("run");r.querySelector(".mark").innerHTML=RING;pending.push(r);}}
 }
 if(traceEl)traceEl.querySelector(".tmeta").textContent=nSteps?"· "+plural(nSteps,"step"):"";
 maybeBottom();
}
function finishTrace(status){
 pending.forEach(r=>settle(r,"",null,""));pending=[];
 if(!traceEl)return;
 if(!traceBody.querySelector(".step")){traceEl.closest(".msg").remove();traceEl=traceBody=null;return;}
 const bad=status==="error";
 traceEl.querySelector(".ic").innerHTML=bad?IC.x:IC.check;
 traceEl.querySelector(".tlabel").textContent=plural(nSteps,"step");
 traceEl.querySelector(".tmeta").textContent=nCmds?"· "+plural(nCmds,"command"):"";
 // A failed run keeps its timeline open: the steps are how you see what went wrong.
 if(bad)traceEl.classList.add("bad");else traceEl.classList.add("collapsed");
}

// ---- header: who is working, a status chip with a live clock (ticks locally
// between polls) and the time budget, and a progress bar against that budget.
let clk={state:""},tickT=null;
function paintClock(){
 if(clk.state!=="working")return;
 const e=clk.start>0?Math.max(clk.elapsed,Date.now()/1000-clk.start):clk.elapsed;
 $("st").textContent="Working · "+fmtS(e);
 $("budget").textContent=clk.timeout>0?"/ "+fmtS(clk.timeout):"";
 const fr=clk.timeout>0?Math.min(e/clk.timeout,.98):.05,g=$("gfill");
 g.style.width=(fr*100).toFixed(1)+"%";g.className="gfill"+(fr>.75?" warn":"");
 $("track").title=clk.timeout>0?fmtS(e)+" of the "+fmtS(clk.timeout)+" time budget":"";
}
function setIcon(k,html){const i=$("chipic");if(i.dataset.k!==k){i.innerHTML=html;i.dataset.k=k;}}
// state: working | queued | done | error | lost | idle. o: {start, timeout, elapsed}.
function setState(state,o){
 o=o||{};clk={state,start:o.start||0,timeout:o.timeout||0,elapsed:o.elapsed||0};
 const g=$("gfill");$("chip").className="chip "+state;
 if(state==="working"){setIcon("w",RING);if(!tickT)tickT=setInterval(paintClock,100);paintClock();}
 else{
  if(tickT){clearInterval(tickT);tickT=null;}
  $("budget").textContent="";
  setIcon(state,{done:IC.check,error:IC.x,lost:IC.warn}[state]||IC.clock);
  if(state==="done"||state==="error"){
   $("st").textContent=(state==="done"?"Done":"Failed")+" · "+fmtS(o.elapsed);
   g.style.width="100%";g.className="gfill"+(state==="error"?" error":"");
  }else if(state==="lost")$("st").textContent="Disconnected";
  else{$("st").textContent=state==="queued"?"Queued":"Idle";g.style.width="0";g.className="gfill";}
 }
 document.title=({working:"● ",done:"✓ ",error:"✗ ",lost:"⚠ "}[state]||"")+TITLE;
}
function setAgent(b,repo){
 AGENT=bkName(b);
 const av=$("av");if(av.dataset.n!==AGENT){av.dataset.n=AGENT;av.className="av big "+AGENT;paintAvatar(av,AGENT);}
 $("bkname").textContent=NAMES[AGENT]||AGENT;
 const r=$("repo");r.hidden=!repo;if(repo){r.textContent=repo;r.title=repo;}
}
"""


def page(title: str, script: str, css: str = "", body: str = "") -> str:
    """A complete chat-window page: the shared core plus a caller's poll loop and
    any page-only CSS or markup."""
    return (
        '<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="google" content="notranslate">\n'
        f"<title>{title}</title><style>{BASE_CSS}{CSS}{css}</style></head><body>{BODY}{body}"
        f"<script>{BASE_JS}{JS}{script}</script></body></html>"
    )
