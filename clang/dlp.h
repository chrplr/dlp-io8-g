#ifndef DLP_H
#define DLP_H

#include <stdbool.h>
#include <stddef.h>

typedef struct {
    int fd;
} dlp_io8g_t;

/**
 * @brief Initialize and open the DLP-IO8-G device.
 * 
 * @param device The device path (e.g., "/dev/ttyUSB0").
 * @param baudrate The baud rate (e.g., 9600).
 * @return dlp_io8g_t* Pointer to the initialized structure, or NULL on error.
 */
dlp_io8g_t* dlp_new(const char* device, int baudrate);

/**
 * @brief Close the device and free resources.
 * 
 * @param dlp Pointer to the device structure.
 */
void dlp_close(dlp_io8g_t* dlp);

/**
 * @brief Ping the device to check if it's responsive.
 * 
 * @param dlp Pointer to the device structure.
 * @return true if the device responded correctly, false otherwise.
 */
bool dlp_ping(dlp_io8g_t* dlp);

/**
 * @brief Read the states of all 8 lines.
 * 
 * @param dlp Pointer to the device structure.
 * @param states Array of at least 8 bytes to store the results.
 * @return size_t Number of bytes read (should be 8), or 0 on error.
 */
size_t dlp_read(dlp_io8g_t* dlp, unsigned char* states);

/**
 * @brief Set lines to 1.
 * 
 * @param dlp Pointer to the device structure.
 * @param lines String specifying lines to set (e.g., "1234").
 */
void dlp_set(dlp_io8g_t* dlp, const char* lines);

/**
 * @brief Set lines to 0.
 * 
 * @param dlp Pointer to the device structure.
 * @param lines String specifying lines to unset (e.g., "1234").
 */
void dlp_unset(dlp_io8g_t* dlp, const char* lines);

#endif // DLP_H
