package service

import (
	"os"
	"testing"

	"github.com/gin-gonic/gin"
)

// Gin's process-wide mode is not safe to mutate from parallel tests. Establish
// it once before the package test suite starts.
func TestMain(m *testing.M) {
	gin.SetMode(gin.TestMode)
	os.Exit(m.Run())
}
